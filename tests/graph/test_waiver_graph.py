import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ffagent.domain.models import LeagueRef, Platform
from ffagent.graphs.lineup import LineupDeps
from ffagent.graphs.waivers import build_waiver_graph, target_week, thread_id
from ffagent.providers.sleeper import SleeperProvider
from ffagent.schedule.nfl import parse_scoreboard
from ffagent.sources.projections import parse_projections
from ffagent.sources.values import ValueGateway
from ffagent.store.db import Store
from tests.graph.test_lineup_graph import FakeProjections
from tests.providers.test_sleeper import FakeClient

FIX = Path(__file__).parent.parent / "fixtures"
TUESDAY = "2026-10-13T09:00:00-06:00"   # after week 5's Monday game


def make_deps(store=None):
    sched = parse_scoreboard(json.loads((FIX / "espn" / "scoreboard_2026_w5.json").read_text()))
    proj = FakeProjections(parse_projections(json.loads((FIX / "sleeper" / "projections_w5.json").read_text())))
    provider = SleeperProvider(FakeClient())
    values = ValueGateway(proj, None, "Sleeper")
    return LineupDeps(provider, proj, lambda s, w: sched, store=store, values=values)


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.SLEEPER, league_id="1000000000000000001", season=2026, my_team_id="1", name="T")


def run(graph, ref, now=TUESDAY):
    cfg = {"configurable": {"thread_id": thread_id(ref, 6)}}
    return graph.invoke({"league": ref.model_dump(mode="json"), "week": 5, "now": now}, cfg), cfg


def test_target_week_rolls_over_after_the_last_game():
    deps = make_deps()
    assert target_week(deps.schedule_for_week, 2026, 5, __import__("datetime").datetime.fromisoformat(TUESDAY)) == 6
    assert target_week(deps.schedule_for_week, 2026, 5, __import__("datetime").datetime.fromisoformat("2026-10-11T09:00:00-06:00")) == 5


def test_proposes_claims_with_drops_and_bids(ref):
    graph = build_waiver_graph(make_deps(Store()), MemorySaver())
    out, _ = run(graph, ref)
    assert out["week"] == 6
    s = out["summary"]
    assert s["waiver_type"] == "faab" and s["budget"] == 77 and s["values"].startswith("values: Sleeper")
    assert s["drop_candidates"]
    props = out["__interrupt__"][0].value["proposals"]
    assert props and all(p["kind"] == "waiver_claim" for p in props)
    for p in props:
        assert p["payload"]["bid"] is not None and p["payload"]["player_out"] is not None
        assert "ppg ROS" in p["evidence"][0] and p["payload"]["step"].startswith("bid ")


def test_approved_claim_is_verified_next_run(ref):
    store = Store()
    graph = build_waiver_graph(make_deps(store), MemorySaver())
    out, cfg = run(graph, ref)
    props = out["__interrupt__"][0].value["proposals"]
    graph.invoke(Command(resume=[{"action_id": p["id"], "verdict": "approved" if i == 0 else "rejected"} for i, p in enumerate(props)]), cfg)
    graph2 = build_waiver_graph(make_deps(store), MemorySaver())
    out2, _ = run(graph2, ref)
    assert any("not on roster" in v for v in out2["summary"]["verify"])
    # the rejected claim is not re-proposed
    again = [p["payload"]["player_in"] for p in out2.get("__interrupt__", [None])[0].value["proposals"]] if "__interrupt__" in out2 else []
    assert props[1]["payload"]["player_in"] not in again


def test_commissioner_league_is_skipped(ref):
    ref.my_team_id = None
    out, _ = run(build_waiver_graph(make_deps(), MemorySaver()), ref)
    assert "waiver workflow skipped" in out["error"]
