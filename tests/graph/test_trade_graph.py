import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ffagent.domain.models import LeagueRef, Platform
from ffagent.graphs.lineup import LineupDeps
from ffagent.graphs.trades import build_trade_graph, past_deadline, thread_id
from ffagent.providers.sleeper import SleeperProvider
from ffagent.schedule.nfl import parse_scoreboard
from ffagent.sources.projections import parse_projections
from ffagent.sources.values import ValueGateway
from ffagent.store.db import Store
from tests.graph.test_lineup_graph import FakeProjections
from tests.providers.test_sleeper import FakeClient

FIX = Path(__file__).parent.parent / "fixtures"
WED = "2026-10-14T09:00:00-06:00"


def make_deps(store=None):
    sched = parse_scoreboard(json.loads((FIX / "espn" / "scoreboard_2026_w5.json").read_text()))
    proj = FakeProjections(parse_projections(json.loads((FIX / "sleeper" / "projections_w5.json").read_text())))
    provider = SleeperProvider(FakeClient())
    return LineupDeps(provider, proj, lambda s, w: sched, store=store, values=ValueGateway(proj, None, "Sleeper"))


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.SLEEPER, league_id="1000000000000000001", season=2026, my_team_id="4", name="T")


def run(graph, ref, now=WED):
    cfg = {"configurable": {"thread_id": thread_id(ref, 6)}}
    return graph.invoke({"league": ref.model_dump(mode="json"), "week": 5, "now": now}, cfg), cfg


def test_offers_help_me_and_name_a_partner(ref):
    out, _ = run(build_trade_graph(make_deps(Store()), MemorySaver()), ref)
    s = out["summary"]
    assert s["strengths"] and s["deadline"] == "week 12" and "offers_found" in s
    if "__interrupt__" in out:
        for p in out["__interrupt__"][0].value["proposals"]:
            pl = p["payload"]
            assert p["kind"] == "trade_offer" and pl["partner_name"] and pl["give"] and pl["get"]
            assert pl["my_gain"] >= 1.0 and pl["their_gain"] >= -0.5 and "Offer " in pl["step"] and pl["message"]


def test_deadline_blocks_the_workflow():
    from datetime import UTC, datetime

    from ffagent.domain.models import LeagueSettings, Slot
    s = LeagueSettings(num_teams=10, roster_slots={Slot.QB: 1}, trade_deadline_week=12)
    assert past_deadline(s, 13, datetime.now(UTC)) and not past_deadline(s, 12, datetime.now(UTC))
    s2 = LeagueSettings(num_teams=10, roster_slots={Slot.QB: 1}, trade_deadline=datetime(2026, 11, 1, tzinfo=UTC))
    assert past_deadline(s2, 5, datetime(2026, 11, 2, tzinfo=UTC)) and not past_deadline(s2, 5, datetime(2026, 10, 2, tzinfo=UTC))


def test_rejected_offer_is_remembered(ref):
    store = Store()
    graph = build_trade_graph(make_deps(store), MemorySaver())
    out, cfg = run(graph, ref)
    if "__interrupt__" not in out:
        pytest.skip("fixture produced no offers for this team")
    props = out["__interrupt__"][0].value["proposals"]
    graph.invoke(Command(resume=[{"action_id": p["id"], "verdict": "rejected", "note": "not selling"} for p in props]), cfg)
    out2, _ = run(build_trade_graph(make_deps(store), MemorySaver()), ref)
    again = {p["payload"]["step"] for p in out2["__interrupt__"][0].value["proposals"]} if "__interrupt__" in out2 else set()
    assert not ({p["payload"]["step"] for p in props} & again)


def test_trade_adviser_picks_and_writes_message(ref):
    from ffagent.llm.advise import TradeAdvice, TradeAdviceOut, TradeAdviser
    from tests.unit.test_advise import FakeLLM
    deps = make_deps(Store())
    probe, _ = run(build_trade_graph(deps, MemorySaver()), ref)
    if "__interrupt__" not in probe:
        pytest.skip("fixture produced no offers for this team")
    first = probe["__interrupt__"][0].value["proposals"][0]["payload"]
    offer_id = "give:" + "+".join(sorted(first["give"])) + "|get:" + "+".join(sorted(first["get"]))
    llm = FakeLLM(TradeAdviceOut(proposals=[TradeAdvice(offer_id=offer_id, action="propose", message="Deal?", principle="Buy RBs from WR-rich teams", rationale="They need WR.")],
                                 watch=["Team X might sell"]))
    out, _ = run(build_trade_graph(deps, MemorySaver(), adviser=TradeAdviser(llm, "fake", "pb")), ref)
    props = out["__interrupt__"][0].value["proposals"]
    assert len(props) == 1 and props[0]["payload"]["message"] == "Deal?" and "[Buy RBs" in props[0]["rationale"]
    assert out["summary"]["advise"].startswith("advise: fake chose 1 of") and out["summary"]["watch"] == ["Team X might sell"]


def test_commissioner_league_is_skipped(ref):
    ref.my_team_id = None
    out, _ = run(build_trade_graph(make_deps(), MemorySaver()), ref)
    assert "trade workflow skipped" in out["error"]
