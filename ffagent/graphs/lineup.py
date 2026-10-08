"""Lineup workflow: load → analyze → human_review (interrupt) → checklist.

One graph invocation per (league, week). The thread id makes reruns resume instead of duplicating.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ffagent.analysis.context import lineup_candidates
from ffagent.analysis.lineup import optimize_lineup
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import Decision, LeagueRef, ProposedAction


class LineupState(TypedDict, total=False):
    league: dict            # LeagueRef
    week: int
    now: str                # ISO timestamp; injected so runs are reproducible
    summary: dict           # before/after points, flags, warnings, close calls
    proposals: list[dict]   # ProposedAction
    decisions: list[dict]   # Decision
    checklist: list[str]
    error: str


class LineupDeps:
    """Everything the graph needs from the outside world, so tests can hand in fixtures."""

    def __init__(self, provider, projections, schedule_for_week, close_margin: float = 1.0):
        self.provider = provider
        self.projections = projections          # .week(ref, week) -> dict[player_id, Projection]
        self.schedule_for_week = schedule_for_week  # (season, week) -> WeekSchedule
        self.close_margin = close_margin


def _name_ids(text: str, name) -> str:
    """Optimizer messages carry player ids; swap the leading id for a name."""
    head, _, rest = text.partition(" ")
    return f"{name(head)} {rest}" if name(head) != head else text


def thread_id(ref: LeagueRef, week: int) -> str:
    return f"lineup:{ref.key}:{ref.season}:{week}"


def build_lineup_graph(deps: LineupDeps, checkpointer: BaseCheckpointSaver | None = None):
    def analyze(state: LineupState) -> dict[str, Any]:
        ref = LeagueRef.model_validate(state["league"])
        week = state["week"]
        now = datetime.fromisoformat(state["now"]).astimezone(UTC)
        if ref.my_team_id is None:
            return {"error": "no team of mine in this league (commissioner view); lineup workflow skipped", "proposals": []}
        settings = deps.provider.settings(ref)
        team = next(t for t in deps.provider.teams(ref) if t.id == ref.my_team_id)
        index = PlayerIndex(deps.provider.players())
        schedule = deps.schedule_for_week(ref.season, week)
        projections = deps.projections.week(ref, week)
        cands = lineup_candidates(team, settings, index, projections, schedule, now)
        result = optimize_lineup(settings.starting_slots, cands, close_margin=deps.close_margin)

        name = lambda pid: index.by_id(pid).name if pid and index.by_id(pid) else (pid or "(empty)")
        by_id = {c.player.id: c for c in cands}
        proposals = []
        for n, (pin, pout, slot) in enumerate(result.moves, 1):
            cin, cout = by_id[pin], by_id.get(pout) if pout else None
            game = schedule.game_for(cin.player.team) if cin.player.team else None
            opp = next((t for t in game.teams if t != cin.player.team), None) if game else None
            ev = [f"{name(pin)} projects {cin.points} ({cin.player.position}, {cin.player.team} vs {opp or 'bye'})"]
            if cout:
                ev.append(f"{name(pout)} projects {cout.points}" + (f", {cout.reason}" if cout.reason else ""))
            proposals.append(ProposedAction(
                id=f"{thread_id(ref, week)}:{n}", kind="lineup_swap", league_key=ref.key,
                payload={"slot": slot, "player_in": pin, "player_in_name": name(pin),
                         "player_out": pout, "player_out_name": name(pout) if pout else None,
                         "locks_at": game.kickoff.isoformat() if game else None},
                rationale=f"Start {name(pin)} at {slot}" + (f" over {name(pout)}" if pout else " (slot is empty)"),
                evidence=ev, deadline=game.kickoff if game else None,
            ).model_dump(mode="json"))
        summary = {"team": team.name, "projected_before": round(result.projected_before, 2),
                   "projected_after": round(result.projected_after, 2),
                   "warnings": [_name_ids(w, name) for w in result.warnings],
                   "flags": [f"{name(pid)} is {status} ({reason})" for pid, status, reason in result.flags],
                   "close_calls": [f"{name(a)} over {name(b)} at {s} by {m:.1f}" for a, b, s, m in result.close_calls],
                   "first_kickoff": schedule.first_kickoff.isoformat()}
        return {"proposals": proposals, "summary": summary}

    def human_review(state: LineupState) -> dict[str, Any]:
        if not state.get("proposals"):
            return {"decisions": []}
        raw = interrupt({"league": state["league"], "week": state["week"],
                         "summary": state["summary"], "proposals": state["proposals"]})
        decisions = [Decision.model_validate(d).model_dump(mode="json") for d in raw]
        ids = {p["id"] for p in state["proposals"]}
        missing = ids - {d["action_id"] for d in decisions}
        if missing:
            raise ValueError(f"no decision for {sorted(missing)}")
        return {"decisions": decisions}

    def checklist(state: LineupState) -> dict[str, Any]:
        verdicts = {d["action_id"]: d for d in state.get("decisions", [])}
        steps = []
        for p in state.get("proposals", []):
            d = verdicts.get(p["id"])
            if d and d["verdict"] in ("approved", "edited"):
                pl = p["payload"] | (d.get("edited_payload") or {})
                out = f" (bench {pl['player_out_name']})" if pl.get("player_out") else ""
                steps.append(f"{pl['slot']}: start {pl['player_in_name']}{out}")
        return {"checklist": steps}

    def route(state: LineupState) -> str:
        return END if state.get("error") else "human_review"

    g = StateGraph(LineupState)
    g.add_node("analyze", analyze)
    g.add_node("human_review", human_review)
    g.add_node("checklist", checklist)
    g.add_edge(START, "analyze")
    g.add_conditional_edges("analyze", route, {END: END, "human_review": "human_review"})
    g.add_edge("human_review", "checklist")
    g.add_edge("checklist", END)
    return g.compile(checkpointer=checkpointer)
