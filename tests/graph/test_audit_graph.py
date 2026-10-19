import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from ffagent.domain.models import InjuryStatus, LeagueRef, Platform
from ffagent.graphs.audit import build_audit_graph, thread_id
from ffagent.graphs.lineup import LineupDeps
from ffagent.providers.sleeper import SleeperProvider
from ffagent.schedule.nfl import parse_scoreboard
from ffagent.sources.projections import parse_projections
from ffagent.sources.values import ValueGateway
from ffagent.store.db import Store
from tests.graph.test_lineup_graph import FakeProjections
from tests.providers.test_sleeper import FakeClient

FIX = Path(__file__).parent.parent / "fixtures"
SAT = "2026-10-10T12:00:00-06:00"


def make_deps(store=None):
    sched = parse_scoreboard(json.loads((FIX / "espn" / "scoreboard_2026_w5.json").read_text()))
    proj = FakeProjections(parse_projections(json.loads((FIX / "sleeper" / "projections_w5.json").read_text())))
    provider = SleeperProvider(FakeClient())
    return LineupDeps(provider, proj, lambda s, w: sched, store=store, values=ValueGateway(proj, None, "Sleeper"))


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.SLEEPER, league_id="1000000000000000001", season=2026, my_team_id="1", name="T")


def run(graph, ref, now=SAT):
    from datetime import datetime
    cfg = {"configurable": {"thread_id": thread_id(ref, 5, datetime.fromisoformat(now))}}
    return graph.invoke({"league": ref.model_dump(mode="json"), "week": 5, "now": now}, cfg), cfg


def test_empty_slot_and_injured_starter_produce_notices(ref):
    deps = make_deps(Store())
    # Fixture: team 2 starts nobody at DEF. Also hurt a starter on team 3.
    t3 = next(t for t in deps.provider.teams(ref) if t.id == "3")
    for p in deps.provider.players():
        if p.id == t3.starters[1].player_id:
            p.injury_status = InjuryStatus.OUT
    out, _ = run(build_audit_graph(deps, MemorySaver()), ref)
    s = out["summary"]
    assert s["teams_checked"] == 9 and any("DEF empty slot" in line for line in s["report"])
    props = out["__interrupt__"][0].value["proposals"]
    by_team = {p["payload"]["team_id"]: p for p in props}
    assert "2" in by_team and "3" in by_team and "1" not in by_team
    assert by_team["3"]["payload"]["message"].startswith("Hey manager3, heads up")
    assert all(p["kind"] == "manager_notice" and p["payload"]["step"].startswith("Message ") for p in props)


def test_commissioner_league_audits_everyone(ref):
    ref.my_team_id = None
    out, _ = run(build_audit_graph(make_deps(Store()), MemorySaver()), ref)
    assert out["summary"]["teams_checked"] == 10


def test_approved_notice_is_not_repeated_without_new_issues(ref):
    store = Store()
    graph = build_audit_graph(make_deps(store), MemorySaver())
    out, cfg = run(graph, ref)
    props = out["__interrupt__"][0].value["proposals"]
    graph.invoke(Command(resume=[{"action_id": p["id"], "verdict": "approved"} for p in props]), cfg)
    out2, _ = run(build_audit_graph(make_deps(store), MemorySaver()), ref, now="2026-10-10T14:00:00-06:00")
    assert "__interrupt__" not in out2 and out2["summary"]["report"]  # still reported, not re-asked


def test_include_me(ref):
    deps = make_deps(Store())
    me = next(t for t in deps.provider.teams(ref) if t.id == "1")
    for p in deps.provider.players():
        if p.id == me.starters[0].player_id:
            p.injury_status = InjuryStatus.OUT
    out, _ = run(build_audit_graph(deps, MemorySaver(), include_me=True), ref)
    assert any(p["payload"]["team_id"] == "1" for p in out["__interrupt__"][0].value["proposals"])
