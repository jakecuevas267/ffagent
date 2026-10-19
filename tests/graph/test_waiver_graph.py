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
        assert p["payload"]["step"].startswith("bid ")
        if p["payload"]["role"] != "stream":
            assert "ppg ROS" in p["evidence"][0]


def test_approved_claim_is_verified_next_run(ref):
    store = Store()
    graph = build_waiver_graph(make_deps(store), MemorySaver())
    out, cfg = run(graph, ref)
    props = out["__interrupt__"][0].value["proposals"]
    graph.invoke(Command(resume=[{"action_id": p["id"], "verdict": "approved" if i == 0 else "rejected"} for i, p in enumerate(props)]), cfg)
    graph2 = build_waiver_graph(make_deps(store), MemorySaver())
    out2, _ = run(graph2, ref)
    assert any("not on roster" in v for v in out2["summary"]["verify"])
    # rejected claims (if any) are not re-proposed
    again = [p["payload"]["player_in"] for p in out2["__interrupt__"][0].value["proposals"]] if "__interrupt__" in out2 else []
    assert all(p["payload"]["player_in"] not in again for p in props[1:])


def test_commissioner_league_is_skipped(ref):
    ref.my_team_id = None
    out, _ = run(build_waiver_graph(make_deps(), MemorySaver()), ref)
    assert "waiver workflow skipped" in out["error"]


class FakeTrends:
    def __init__(self, add_ids, drop_ids=()):
        self.add_ids, self.drop_ids = add_ids, drop_ids

    def for_league(self, kind, ref, index):
        from ffagent.domain.models import Trend
        ids = self.add_ids if kind == "add" else self.drop_ids
        return [Trend(player_id=pid, count=700000 - i, label=f"{700000 - i:,} {'adds' if kind == 'add' else 'drops'} in 24h")
                for i, pid in enumerate(ids)]


def test_trending_unprojected_free_agent_is_proposed_as_a_flier(ref):
    deps = make_deps(Store())
    provider = deps.provider
    proj = deps.projections.data
    usable = {"QB", "RB", "WR", "TE"}
    flier = next(f for f in provider.free_agents(ref) if f.position in usable)
    proj.pop(flier.id, None)   # no projection at all...
    flier.team = None          # ...because he is between teams
    # The fixture roster is all NFL starters; make its one cuttable bench player a true dead spot
    # (no projection, no rookie ramp, no depth-chart path) so a flier has somewhere to go.
    thomas = next(p for p in provider.players() if p.name == "Brian Thomas")
    proj.pop(thomas.id, None)
    thomas.years_exp, thomas.depth_order = 5, 3
    deps.trends = FakeTrends([flier.id], drop_ids=[provider.teams(ref)[0].roster[0].player_id])
    graph = build_waiver_graph(deps, MemorySaver())
    out, _ = run(graph, ref)
    s = out["summary"]
    assert any(flier.name in line and "no team" in line for line in s["trending"])
    assert s["trending_drops"] and "drops in 24h" in s["trending_drops"][0]
    props = out["__interrupt__"][0].value["proposals"]
    spec = [p for p in props if p["payload"]["role"] == "speculative"]
    assert len(spec) == 1 and spec[0]["payload"]["player_in"] == flier.id
    assert spec[0]["rationale"].startswith("Stash ") and "(flier)" in spec[0]["payload"]["step"]
    assert spec[0]["payload"]["bid"] == 2  # 2% of the 77 FAAB left, rounded


def test_advise_node_proposals_replace_deterministic_ones(ref):
    from ffagent.llm.advise import Advice, AdviceOut, Adviser
    from tests.unit.test_advise import FakeLLM
    deps = make_deps(Store())
    # Find the deterministic candidate/drop pair the graph would offer, then have the "model" pick it with a principle.
    probe = build_waiver_graph(deps, MemorySaver())
    out, _ = run(probe, ref)
    det = next(p for p in out["__interrupt__"][0].value["proposals"] if p["payload"]["role"] != "stream")
    llm = FakeLLM(AdviceOut(proposals=[Advice(add_id=det["payload"]["player_in"], drop_id=det["payload"]["player_out"], action="pickup",
                                               principle="RBs who start for their NFL team are pickups", rationale="He starts and my bench spot is dead.")],
                            watch=["Someone: check Sunday"], holds_questioned=["Nobody"]))
    deps.adviser = Adviser(llm, "fake-model", playbook="- RBs who start for their NFL team are pickups")
    graph = build_waiver_graph(deps, MemorySaver())
    out2, _ = run(graph, ref)
    s = out2["summary"]
    assert s["advise"].startswith("advise: fake-model chose 1 of") and s["watch"] == ["Someone: check Sunday"]
    props = [p for p in out2["__interrupt__"][0].value["proposals"] if p["payload"]["role"] != "stream"]
    assert len(props) == 1 and props[0]["payload"]["principle"] and "[RBs who start" in props[0]["rationale"]
    assert props[0]["payload"]["player_in"] == det["payload"]["player_in"]
    human = llm.messages[1][1]
    assert '"my_roster"' in human and '"pickup_candidates"' in human and '"format": "redraft"' in human


def test_advise_failure_falls_back_to_deterministic(ref):
    from ffagent.llm.advise import Adviser
    from tests.unit.test_advise import FakeLLM
    deps = make_deps(Store())
    deps.adviser = Adviser(FakeLLM(RuntimeError("boom")), "fake-model", "")
    out, _ = run(build_waiver_graph(deps, MemorySaver()), ref)
    assert "showing the deterministic proposals" in out["summary"]["advise"]
    assert out["__interrupt__"][0].value["proposals"]


def test_defense_stream_is_proposed_outside_the_claim_limit(ref):
    deps = make_deps(Store())
    # Give the expert ranks: my Bills DEF ranked 10th, an available defense ranked 2nd.
    provider = deps.provider
    avail_def = next(f for f in provider.free_agents(ref) if f.position == "DEF")

    class FakeExpert:
        name = "X"
        def season(self, ref): return {}
        def ros_ranks(self, ref): return {}
        def weekly_ranks(self, ref, week, position="DST"): return {"BUF": 10, avail_def.id: 2}
    deps.values.expert = FakeExpert()
    graph = build_waiver_graph(deps, MemorySaver(), max_claims=1)
    out, _ = run(graph, ref)
    props = out["__interrupt__"][0].value["proposals"]
    stream = [p for p in props if p["payload"]["role"] == "stream"]
    assert len(stream) == 1 and stream[0]["payload"]["player_in"] == avail_def.id and stream[0]["payload"]["slot"] == "DEF"
    assert "ranked #2" in stream[0]["rationale"] and out["summary"]["stream"].startswith("DEF stream: ")
    assert len(props) == 2  # the stream plus the one allowed claim


def test_defense_stream_can_be_switched_off(ref):
    deps = make_deps(Store())
    out, _ = run(build_waiver_graph(deps, MemorySaver(), stream_defense=False), ref)
    assert out["summary"]["stream"] is None
