"""League audit: every team's lineup checked for byes, injuries and empty slots; a notice per manager."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from ffagent.analysis.audit import audit_team, notice_message
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueRef, ProposedAction
from ffagent.graphs.common import ReviewState, build_review_graph
from ffagent.graphs.facts import enrich
from ffagent.graphs.lineup import LineupDeps
from ffagent.store.db import fingerprint


def thread_id(ref: LeagueRef, week: int, now: datetime) -> str:
    return f"audit:{ref.key}:{ref.season}:{week}:{now.strftime('%Y%m%dT%H%M')}"


def build_audit_graph(deps: LineupDeps, checkpointer: BaseCheckpointSaver | None = None, include_me: bool = False):
    def analyze(state: ReviewState) -> dict[str, Any]:
        ref = LeagueRef.model_validate(state["league"])
        week = state["week"]
        now = datetime.fromisoformat(state["now"]).astimezone(UTC)
        from ffagent.graphs.waivers import target_week
        p = deps.provider
        settings = p.settings(ref)
        week = target_week(deps.schedule_for_week, ref.season, week, now)  # after the last game, audit the coming week
        teams = p.teams(ref)
        index = PlayerIndex(p.players())
        enrich(deps, ref, index, [])
        schedule = deps.schedule_for_week(ref.season, week)
        lineups = p.lineups(ref, week)
        values = {}
        if deps.values is not None:
            try:
                values = deps.values.values(ref, week, settings.scoring)
            except Exception:  # noqa: BLE001 - fixes fall back to any eligible bench player
                values = {}

        rejected = deps.store.rejected_fingerprints(ref.key, week) if deps.store is not None else set()
        already: dict[str, set[str]] = {}
        if deps.store is not None:
            for pl in deps.store.approved_payloads(ref.key, week, "audit"):
                already.setdefault(pl["team_id"], set()).update(pl.get("issue_keys", []))

        proposals, report, skipped_locked = [], [], 0
        for t in teams:
            if t.id == ref.my_team_id and not include_me:
                continue
            issues = audit_team(t, lineups.get(t.id, t.starters), index, schedule, now, values)
            if not issues:
                continue
            line = f"{t.name} ({t.owner}): " + "; ".join(
                f"{i.player_name} {i.slot} {i.detail}" + (" [locked]" if i.locked else f" → {i.fix}" if i.fix else " (no bench fix)") for i in issues)
            report.append(line)
            fixable = [i for i in issues if not i.locked]
            if not fixable:
                skipped_locked += 1
                continue
            keys = sorted(i.key for i in fixable)
            if set(keys) <= already.get(t.id, set()):
                continue  # told them already this week; nothing new
            msg = notice_message(t.owner, t.name, issues)
            payload = {"slot": None, "player_in": t.id, "player_out": "|".join(keys), "team_id": t.id, "team_name": t.name,
                       "owner": t.owner, "issue_keys": keys, "issues": [i.detail for i in fixable], "message": msg,
                       "player_in_name": t.name, "player_out_name": None,
                       "step": f"Message {t.owner or t.name}: \"{msg}\""}
            if fingerprint("manager_notice", payload) in rejected:
                continue
            proposals.append(ProposedAction(
                id=f"{thread_id(ref, week, now)}:{t.id}", kind="manager_notice", league_key=ref.key, payload=payload,
                rationale=f"{t.name} is starting " + "; ".join(f"{i.player_name} ({i.slot}, {i.detail})" if i.kind != "empty" else f"nobody at {i.slot}" for i in fixable),
                evidence=[(f"{i.player_name} {i.slot}: {i.detail}" + (f"; bench fix: {i.fix}" if i.fix else "; no eligible bench player")) for i in fixable],
            ).model_dump(mode="json"))

        summary = {"team": "league", "week": week, "teams_checked": len(teams) - (0 if include_me or ref.my_team_id is None else 1),
                   "report": report, "locked_only": skipped_locked, "projected_before": 0, "projected_after": 0,
                   "first_kickoff": schedule.first_kickoff.isoformat(), "warnings": [], "flags": [], "close_calls": []}
        return {"proposals": proposals, "summary": summary, "week": week}

    def record(state: ReviewState) -> dict[str, Any]:
        if deps.store is not None and state.get("proposals"):
            ref = LeagueRef.model_validate(state["league"])
            deps.store.record_decisions(ref.key, state["week"], "audit", state["proposals"], state.get("decisions", []),
                                        now=datetime.fromisoformat(state["now"]))
        return {}

    return build_review_graph(analyze, checkpointer, record)
