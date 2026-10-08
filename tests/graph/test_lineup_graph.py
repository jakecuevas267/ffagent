import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ffagent.domain.models import LeagueRef, Platform
from ffagent.graphs.lineup import LineupDeps, build_lineup_graph, thread_id
from ffagent.providers.sleeper import SleeperProvider
from ffagent.schedule.nfl import parse_scoreboard
from ffagent.sources.projections import parse_projections
from tests.providers.test_sleeper import FakeClient

FIX = Path(__file__).parent.parent / "fixtures"
NOW = "2026-10-07T18:00:00-06:00"  # Wednesday evening before the week-5 Thursday game


class FakeProjections:
    def __init__(self, data):
        self.data = data

    def week(self, ref, week):
        return self.data


@pytest.fixture
def deps():
    sched = parse_scoreboard(json.loads((FIX / "espn" / "scoreboard_2026_w5.json").read_text()))
    proj = parse_projections(json.loads((FIX / "sleeper" / "projections_w5.json").read_text()))
    return LineupDeps(SleeperProvider(FakeClient()), FakeProjections(proj), lambda s, w: sched)


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.SLEEPER, league_id="1000000000000000001", season=2026,
                     my_team_id="1", name="Test League")


def run(graph, ref, week=5):
    cfg = {"configurable": {"thread_id": thread_id(ref, week)}}
    out = graph.invoke({"league": ref.model_dump(mode="json"), "week": week, "now": NOW}, cfg)
    return out, cfg


def test_interrupts_with_proposals(deps, ref):
    graph = build_lineup_graph(deps, MemorySaver())
    out, _ = run(graph, ref)
    assert "__interrupt__" in out
    payload = out["__interrupt__"][0].value
    assert payload["league"]["league_id"] == ref.league_id
    assert payload["summary"]["projected_after"] >= payload["summary"]["projected_before"]
    assert payload["proposals"], "fixture roster is deliberately suboptimal"
    p = payload["proposals"][0]
    assert p["kind"] == "lineup_swap" and p["payload"]["player_in_name"] and p["evidence"]
    assert " vs " in p["evidence"][0] and "None" not in p["evidence"][0]


def test_approve_builds_checklist(deps, ref):
    graph = build_lineup_graph(deps, MemorySaver())
    out, cfg = run(graph, ref)
    props = out["__interrupt__"][0].value["proposals"]
    decisions = [{"action_id": p["id"], "verdict": "approved"} for p in props]
    final = graph.invoke(Command(resume=decisions), cfg)
    assert len(final["checklist"]) == len(props)
    assert final["checklist"][0].startswith(props[0]["payload"]["slot"] + ": start ")


def test_reject_leaves_checklist_empty_and_records_decision(deps, ref):
    graph = build_lineup_graph(deps, MemorySaver())
    out, cfg = run(graph, ref)
    props = out["__interrupt__"][0].value["proposals"]
    decisions = [{"action_id": p["id"], "verdict": "rejected", "note": "riding the hot hand"} for p in props]
    final = graph.invoke(Command(resume=decisions), cfg)
    assert final["checklist"] == []
    assert final["decisions"][0]["note"] == "riding the hot hand"


def test_missing_decision_is_an_error(deps, ref):
    graph = build_lineup_graph(deps, MemorySaver())
    _, cfg = run(graph, ref)
    with pytest.raises(ValueError):
        graph.invoke(Command(resume=[]), cfg)


def test_rerun_of_a_finished_thread_does_not_ask_again(deps, ref):
    graph = build_lineup_graph(deps, MemorySaver())
    out, cfg = run(graph, ref)
    props = out["__interrupt__"][0].value["proposals"]
    graph.invoke(Command(resume=[{"action_id": p["id"], "verdict": "approved"} for p in props]), cfg)
    state = graph.get_state(cfg)
    assert state.next == ()  # finished; the CLI checks this before invoking again
    assert state.values["checklist"]


def test_no_team_in_league_is_reported_not_raised(deps, ref):
    graph = build_lineup_graph(deps, MemorySaver())
    ref.my_team_id = None
    out, _ = run(graph, ref)
    assert "no team of mine" in out["error"] and "__interrupt__" not in out


def test_already_optimal_lineup_does_not_interrupt(deps, ref, monkeypatch):
    from ffagent.analysis import lineup as mod
    real = mod.optimize_lineup

    def no_moves(slots, cands, close_margin=1.0):
        r = real(slots, cands, close_margin)
        r.moves = []
        return r
    monkeypatch.setattr("ffagent.graphs.lineup.optimize_lineup", no_moves)
    graph = build_lineup_graph(deps, MemorySaver())
    out, _ = run(graph, ref)
    assert "__interrupt__" not in out and out["checklist"] == [] and out["proposals"] == []


def test_locked_players_are_not_proposed(deps, ref):
    graph = build_lineup_graph(deps, MemorySaver())
    cfg = {"configurable": {"thread_id": "lineup:late"}}
    late = "2026-10-12T20:00:00-06:00"  # Monday evening: every game but MNF has kicked off
    out = graph.invoke({"league": ref.model_dump(mode="json"), "week": 5, "now": late}, cfg)
    props = out.get("__interrupt__", [None])[0]
    if props is not None:
        for p in props.value["proposals"]:
            assert p["payload"]["locks_at"] > late
