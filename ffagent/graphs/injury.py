"""Game-day injury workflow: necessary moves only, status diffs, rejected-move memory, verification."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from ffagent.analysis.injuries import diff_snapshots, injury_moves, ir_moves, snapshot_of
from ffagent.domain.models import LeagueRef, ProposedAction
from ffagent.graphs.common import ReviewState, build_review_graph
from ffagent.graphs.lineup import (
    LineupDeps,
    load_context,
    pending_verification,
    proposals_from,
    summary_from,
)
from ffagent.store.db import fingerprint


def thread_id(ref: LeagueRef, week: int, now: datetime) -> str:
    return f"injury:{ref.key}:{ref.season}:{week}:{now.strftime('%Y%m%dT%H%M')}"


def build_injury_graph(deps: LineupDeps, checkpointer: BaseCheckpointSaver | None = None):
    def analyze(state: ReviewState) -> dict[str, Any]:
        ref = LeagueRef.model_validate(state["league"])
        week = state["week"]
        now = datetime.fromisoformat(state["now"]).astimezone(UTC)
        if ref.my_team_id is None:
            return {"error": "no team of mine in this league (commissioner view); injury workflow skipped", "proposals": []}
        settings, team, index, schedule, cands = load_context(deps, ref, week, now)
        name = lambda pid: index.by_id(pid).name if index.by_id(pid) else pid

        snap = snapshot_of(cands, now)
        changes = []
        if deps.store is not None:
            prev = deps.store.latest_snapshot(ref.key, week)
            changes = [f"{name(pid)}: {old or 'unlisted'} → {new} ({kind})" for pid, old, new, kind in diff_snapshots(prev, snap)]
            deps.store.save_snapshot(ref.key, week, snap)

        result = injury_moves(settings.starting_slots, cands)
        proposals = proposals_from(result, cands, index, schedule, ref, week, thread_id(ref, week, now))
        rejected = deps.store.rejected_fingerprints(ref.key, week) if deps.store is not None else set()
        proposals = [p for p in proposals if fingerprint(p["kind"], p["payload"]) not in rejected]
        for p in proposals:
            out = next((c for c in cands if c.player.id == p["payload"]["player_out"]), None)
            if out is not None:
                p["rationale"] = f"{name(out.player.id)} is {out.reason or out.player.injury_status}: start {p['payload']['player_in_name']} at {p['payload']['slot']}"

        for n, m in enumerate(ir_moves(team, index, settings), 1):
            pname = name(m.player_id)
            if m.direction == "to_ir":
                rationale, step = f"Move {pname} to IR ({m.reason})", f"IR: move {pname} to IR"
            else:
                rationale, step = f"Activate {pname} from IR ({m.reason})", f"IR: activate {pname} (needs a bench spot)"
            pa = ProposedAction(id=f"{thread_id(ref, week, now)}:ir{n}", kind="ir_move", league_key=ref.key,
                                payload={"slot": "IR", "player_in": m.player_id if m.direction == "to_ir" else None,
                                         "player_out": m.player_id if m.direction == "from_ir" else None,
                                         "direction": m.direction, "player_name": pname, "step": step},
                                rationale=rationale, evidence=[m.reason])
            if fingerprint(pa.kind, pa.payload) not in rejected:
                proposals.append(pa.model_dump(mode="json"))

        summary = summary_from(result, team, index, schedule)
        summary["changes"] = changes
        summary["pending"] = pending_verification(deps, ref, week, team, index)
        # Starters in later games who are not healthy: worth watching, not acting on yet.
        summary["watch"] = [f"{name(c.player.id)} ({c.player.injury_status}, {c.reason or 'see report'}) plays at "
                            f"{schedule.game_for(c.player.team).kickoff.isoformat()}"
                            for c in cands if c.is_current_starter and c.available and not c.locked
                            and c.player.injury_status.value in ("questionable", "doubtful") and c.player.team
                            and schedule.game_for(c.player.team)]
        return {"proposals": proposals, "summary": summary}

    def record(state: ReviewState) -> dict[str, Any]:
        if deps.store is not None and state.get("proposals"):
            ref = LeagueRef.model_validate(state["league"])
            deps.store.record_decisions(ref.key, state["week"], "injury", state["proposals"], state.get("decisions", []),
                                        now=datetime.fromisoformat(state["now"]))
        return {}

    return build_review_graph(analyze, checkpointer, record)
