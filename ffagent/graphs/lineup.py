"""Lineup workflow (Thursday lock). One thread per (league, week); reruns resume rather than duplicate."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from ffagent.analysis.context import lineup_candidates
from ffagent.analysis.injuries import verify_lineup
from ffagent.analysis.lineup import LineupResult, optimize_lineup
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueRef, ProposedAction
from ffagent.graphs.common import ReviewState, build_review_graph

LineupState = ReviewState


class LineupDeps:
    """Everything the graphs need from the outside world, so tests can hand in fixtures."""

    def __init__(self, provider, projections, schedule_for_week, close_margin: float = 1.0, store=None, values=None):
        self.provider = provider
        self.projections = projections          # .week(ref, week) -> dict[player_id, Projection]
        self.schedule_for_week = schedule_for_week  # (season, week) -> WeekSchedule
        self.close_margin = close_margin
        self.store = store                      # optional ffagent.store.db.Store
        self.values = values                    # optional ffagent.sources.values.ValueGateway (waivers, trades)


def thread_id(ref: LeagueRef, week: int) -> str:
    return f"lineup:{ref.key}:{ref.season}:{week}"


def _name_ids(text: str, name) -> str:
    """Optimizer messages carry player ids; swap the leading id for a name."""
    head, _, rest = text.partition(" ")
    return f"{name(head)} {rest}" if name(head) != head else text


def load_context(deps: LineupDeps, ref: LeagueRef, week: int, now: datetime):
    settings = deps.provider.settings(ref)
    team = next(t for t in deps.provider.teams(ref) if t.id == ref.my_team_id)
    index = PlayerIndex(deps.provider.players())
    schedule = deps.schedule_for_week(ref.season, week)
    projections = deps.projections.week(ref, week)
    cands = lineup_candidates(team, settings, index, projections, schedule, now)
    return settings, team, index, schedule, cands, projections


def proposals_from(result: LineupResult, cands, index, schedule, ref: LeagueRef, week: int, prefix: str,
                   projections: dict | None = None) -> list[dict]:
    name = lambda pid: index.by_id(pid).name if pid and index.by_id(pid) else (pid or "(empty)")
    src = lambda pid: f" [{projections[pid].source}]" if projections and pid in projections and projections[pid].source else ""
    by_id = {c.player.id: c for c in cands}
    out = []
    for n, (pin, pout, slot) in enumerate(result.moves, 1):
        cin, cout = by_id[pin], by_id.get(pout) if pout else None
        game = schedule.game_for(cin.player.team) if cin.player.team else None
        opp = next((t for t in game.teams if t != cin.player.team), None) if game else None
        ev = [f"{name(pin)} projects {cin.points}{src(pin)} ({cin.player.position}, {cin.player.team} vs {opp or 'bye'})"]
        if cout:
            ev.append(f"{name(pout)} projects {cout.points}{src(pout)}" + (f", {cout.reason}" if cout.reason else ""))
        step = f"{slot}: start {name(pin)}" + (f" (bench {name(pout)})" if pout else "")
        out.append(ProposedAction(
            id=f"{prefix}:{n}", kind="lineup_swap", league_key=ref.key,
            payload={"slot": slot, "player_in": pin, "player_in_name": name(pin), "player_out": pout,
                     "player_out_name": name(pout) if pout else None,
                     "locks_at": game.kickoff.isoformat() if game else None, "step": step},
            rationale=f"Start {name(pin)} at {slot}" + (f" over {name(pout)}" if pout else " (slot is empty)"),
            evidence=ev, deadline=game.kickoff if game else None,
        ).model_dump(mode="json"))
    return out


def sources_line(deps: LineupDeps) -> str | None:
    report = getattr(deps.projections, "report", None)
    if report is None:
        return None
    line = report.line(getattr(deps.projections, "default_name", "platform"))
    if report.unresolved:
        # Few unmatched rows are worth naming (nickname/team mismatches). Many means the platform index
        # only knows rostered players (ESPN), so the rows are simply free agents: just count them.
        if len(report.unresolved) <= 5:
            line += f"; unmatched expert rows: {', '.join(report.unresolved)}"
        else:
            line += f"; {len(report.unresolved)} expert rows not on any known roster"
    return line


def summary_from(result: LineupResult, team, index, schedule) -> dict:
    name = lambda pid: index.by_id(pid).name if pid and index.by_id(pid) else (pid or "(empty)")
    return {"team": team.name, "projected_before": round(result.projected_before, 2),
            "projected_after": round(result.projected_after, 2),
            "warnings": [_name_ids(w, name) for w in result.warnings],
            "flags": [f"{name(pid)} is {status} ({reason})" for pid, status, reason in result.flags],
            "close_calls": [f"{name(a)} over {name(b)} at {s} by {m:.1f}" for a, b, s, m in result.close_calls],
            "first_kickoff": schedule.first_kickoff.isoformat()}


def pending_verification(deps: LineupDeps, ref: LeagueRef, week: int, team, index) -> list[str]:
    """Approved lineup moves from earlier this week that are not reflected in the live lineup."""
    if deps.store is None or ref.my_team_id is None:
        return []
    approved = deps.store.approved_payloads(ref.key, week, "lineup")
    starters: dict = {}
    for e in team.starters:
        starters.setdefault(e.slot, []).append(e.player_id)
    return [p.get("step") or f"{p['slot']}: start {p['player_in_name']}" for p in verify_lineup(approved, starters)]


def build_lineup_graph(deps: LineupDeps, checkpointer: BaseCheckpointSaver | None = None):
    def analyze(state: LineupState) -> dict[str, Any]:
        ref = LeagueRef.model_validate(state["league"])
        week = state["week"]
        now = datetime.fromisoformat(state["now"]).astimezone(UTC)
        if ref.my_team_id is None:
            return {"error": "no team of mine in this league (commissioner view); lineup workflow skipped", "proposals": []}
        settings, team, index, schedule, cands, projections = load_context(deps, ref, week, now)
        result = optimize_lineup(settings.starting_slots, cands, close_margin=deps.close_margin)
        summary = summary_from(result, team, index, schedule)
        summary["pending"] = pending_verification(deps, ref, week, team, index)
        summary["sources"] = sources_line(deps)
        return {"proposals": proposals_from(result, cands, index, schedule, ref, week, thread_id(ref, week), projections),
                "summary": summary}

    def record(state: LineupState) -> dict[str, Any]:
        if deps.store is not None and state.get("proposals"):
            ref = LeagueRef.model_validate(state["league"])
            deps.store.record_decisions(ref.key, state["week"], "lineup", state["proposals"], state.get("decisions", []),
                                        now=datetime.fromisoformat(state["now"]))
        return {}

    return build_review_graph(analyze, checkpointer, record)
