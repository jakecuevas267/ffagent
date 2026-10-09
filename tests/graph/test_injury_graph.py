import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ffagent.domain.models import InjuryStatus, LeagueRef, Platform
from ffagent.graphs.injury import build_injury_graph, thread_id
from ffagent.graphs.lineup import LineupDeps, build_lineup_graph
from ffagent.graphs.lineup import thread_id as lineup_thread
from ffagent.providers.sleeper import SleeperProvider
from ffagent.schedule.nfl import parse_scoreboard
from ffagent.sources.projections import parse_projections
from ffagent.store.db import Store
from tests.graph.test_lineup_graph import FakeProjections
from tests.providers.test_sleeper import FakeClient

FIX = Path(__file__).parent.parent / "fixtures"
SUNDAY_AM = "2026-10-11T09:00:00-06:00"
SUNDAY_PM = "2026-10-11T15:30:00-06:00"  # after the 11am and 2pm MT kickoffs


def make_deps(store, provider=None):
    sched = parse_scoreboard(json.loads((FIX / "espn" / "scoreboard_2026_w5.json").read_text()))
    proj = parse_projections(json.loads((FIX / "sleeper" / "projections_w5.json").read_text()))
    provider = provider or SleeperProvider(FakeClient())
    return LineupDeps(provider, FakeProjections(proj), lambda s, w: sched, store=store)


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.SLEEPER, league_id="1000000000000000001", season=2026, my_team_id="1", name="T")


def injure(provider, player_id, status=InjuryStatus.OUT):
    """Mutate the provider's cached player list so a starter is hurt."""
    for p in provider.players():
        if p.id == player_id:
            p.injury_status = status
            p.injury_note = "test"


def run(graph, ref, now, week=5):
    from datetime import datetime
    cfg = {"configurable": {"thread_id": thread_id(ref, week, datetime.fromisoformat(now))}}
    return graph.invoke({"league": ref.model_dump(mode="json"), "week": week, "now": now}, cfg), cfg


def heal_ir(provider):
    """Fixture has Deebo Samuel healthy in IR; mark him IR so only lineup questions remain."""
    injure(provider, "5872", InjuryStatus.IR)


def test_healthy_lineup_no_interrupt(ref):
    store = Store()
    deps = make_deps(store)
    heal_ir(deps.provider)
    graph = build_injury_graph(deps, MemorySaver())
    out, _ = run(graph, ref, SUNDAY_AM)
    assert "__interrupt__" not in out and out["proposals"] == []
    assert store.latest_snapshot(ref.key, 5) is not None


def test_out_starter_is_proposed_with_replacement(ref):
    store = Store()
    deps = make_deps(store)
    heal_ir(deps.provider)
    injure(deps.provider, "6813")  # Jonathan Taylor, starting RB in the fixture
    graph = build_injury_graph(deps, MemorySaver())
    out, _ = run(graph, ref, SUNDAY_AM)
    props = [p for p in out["__interrupt__"][0].value["proposals"] if p["kind"] == "lineup_swap"]
    assert len(props) == 1 and props[0]["payload"]["player_out"] == "6813" and props[0]["payload"]["slot"] == "RB"
    assert "is out" in props[0]["rationale"]


def test_rejected_move_is_not_asked_again_same_week(ref):
    store = Store()
    deps = make_deps(store)
    heal_ir(deps.provider)
    injure(deps.provider, "6813")
    graph = build_injury_graph(deps, MemorySaver())
    out, cfg = run(graph, ref, SUNDAY_AM)
    props = out["__interrupt__"][0].value["proposals"]
    graph.invoke(Command(resume=[{"action_id": p["id"], "verdict": "rejected", "note": "waiting on warmups"} for p in props]), cfg)
    out2, _ = run(graph, ref, "2026-10-11T10:30:00-06:00")
    assert "__interrupt__" not in out2 and out2["proposals"] == []


def test_status_changes_are_reported(ref):
    store = Store()
    deps = make_deps(store)
    graph = build_injury_graph(deps, MemorySaver())
    run(graph, ref, SUNDAY_AM)
    injure(deps.provider, "6786", InjuryStatus.QUESTIONABLE)  # CeeDee Lamb
    out, _ = run(graph, ref, "2026-10-11T10:30:00-06:00")
    assert any("CeeDee Lamb" in c and "downgrade" in c for c in out["summary"]["changes"])


def test_approved_lineup_move_not_made_shows_as_pending(ref):
    store = Store()
    deps = make_deps(store)
    lineup = build_lineup_graph(deps, MemorySaver())
    cfg = {"configurable": {"thread_id": lineup_thread(ref, 5)}}
    out = lineup.invoke({"league": ref.model_dump(mode="json"), "week": 5, "now": "2026-10-07T18:00:00-06:00"}, cfg)
    props = out["__interrupt__"][0].value["proposals"]
    lineup.invoke(Command(resume=[{"action_id": p["id"], "verdict": "approved"} for p in props]), cfg)
    assert store.approved_payloads(ref.key, 5, "lineup")

    injury = build_injury_graph(deps, MemorySaver())
    out2, _ = run(injury, ref, SUNDAY_AM)
    assert out2["summary"]["pending"] == [p["payload"]["step"] for p in props]


def test_locked_out_starter_is_a_warning_not_a_proposal(ref):
    store = Store()
    deps = make_deps(store)
    heal_ir(deps.provider)
    injure(deps.provider, "6813")  # IND plays at 11am MT; by 3:30pm he's locked
    graph = build_injury_graph(deps, MemorySaver())
    out, _ = run(graph, ref, SUNDAY_PM)
    assert "__interrupt__" not in out  # Deebo fills the only IR slot, so no IR move either
    assert any("Jonathan Taylor" in w and "locked" in w for w in out["summary"]["warnings"])


def test_ir_housekeeping_is_proposed(ref):
    # Fixture: Deebo Samuel (5872) sits in IR but is healthy; the league allows Out on IR with 1 slot.
    store = Store()
    deps = make_deps(store)
    injure(deps.provider, "6813")  # Jonathan Taylor out -> IR-eligible once Deebo is activated
    graph = build_injury_graph(deps, MemorySaver())
    out, _ = run(graph, ref, SUNDAY_AM)
    props = out["__interrupt__"][0].value["proposals"]
    ir = [(p["payload"]["direction"], p["payload"]["player_name"]) for p in props if p["kind"] == "ir_move"]
    assert ir == [("from_ir", "Deebo Samuel"), ("to_ir", "Jonathan Taylor")]
    assert any(p["kind"] == "lineup_swap" for p in props)


def test_ir_rejection_is_remembered(ref):
    store = Store()
    deps = make_deps(store)
    graph = build_injury_graph(deps, MemorySaver())
    out, cfg = run(graph, ref, SUNDAY_AM)
    props = out["__interrupt__"][0].value["proposals"]
    assert [p["kind"] for p in props] == ["ir_move"]  # only Deebo's activation on a healthy lineup
    graph.invoke(Command(resume=[{"action_id": props[0]["id"], "verdict": "rejected", "note": "stashing him"}]), cfg)
    out2, _ = run(graph, ref, "2026-10-11T10:30:00-06:00")
    assert "__interrupt__" not in out2
