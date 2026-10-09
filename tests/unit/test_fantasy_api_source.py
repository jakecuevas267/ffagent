import json
from pathlib import Path

import pytest

from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueRef, Platform, Player, Position
from ffagent.sources.fantasy_api_source import FantasyAPISource, map_projection, resolve
from ffagent.sources.gateway import ProjectionGateway
from ffagent.sources.projections import Projection, score

FIX = Path(__file__).parent.parent / "fixtures"
PROJ = json.loads((FIX / "fantasy_api_source" / "projections_w5.json").read_text())
SCORING = json.loads((FIX / "sleeper" / "league.json").read_text())["scoring_settings"]


class FakeFP:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def projections(self, season, week, position, scoring="PPR"):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429 Too Many Requests")
        return PROJ[position]


@pytest.fixture
def sleeper_index():
    from ffagent.providers.sleeper import map_players
    return PlayerIndex(map_players(json.loads((FIX / "sleeper" / "players.json").read_text())))


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.SLEEPER, league_id="1", season=2026, my_team_id="1")


def test_map_projection_translates_stats_to_sleeper_keys():
    ep = map_projection(PROJ["RB"][0])
    assert ep.position is Position.RB and ep.team
    assert {"rush_yd", "rush_td", "rec", "rec_yd", "rec_td", "fum_lost"} <= set(ep.stats)
    assert "rush_yds" not in ep.stats
    assert score(ep.stats, SCORING) > 5


def test_unknown_position_is_skipped():
    assert map_projection({"name": "X", "position_id": "OL", "stats": {}}) is None


def test_resolve_by_name_position_team(sleeper_index):
    ep = map_projection(PROJ["RB"][0])
    p = resolve(ep, sleeper_index)
    assert p is not None and Position.RB in p.positions


def test_resolve_prefers_yahoo_id():
    ep = map_projection(PROJ["WR"][0])
    ep.yahoo_id = "999"
    idx = PlayerIndex([Player(id="a", name="Someone Else", positions=[Position.WR], team="XX", external_ids={"yahoo": "999"})])
    assert resolve(ep, idx).id == "a"


def test_source_returns_projections_keyed_by_platform_ids(sleeper_index, ref):
    src = FantasyAPISource(FakeFP(), lambda r: sleeper_index)
    out = src.week(ref, 5)
    assert out and all(k == v.player_id for k, v in out.items())
    assert all(v.source == "FantasyAPISource" for v in out.values())
    assert all(sleeper_index.by_id(k) is not None for k in out)


class FakeDefault:
    def __init__(self, data):
        self.data = data

    def week(self, ref, week):
        return self.data


def test_gateway_without_expert_uses_default(ref):
    default = FakeDefault({"x": Projection("x", 5, {"rec": 1})})
    gw = ProjectionGateway(default, None, "Sleeper")
    out = gw.week(ref, 5)
    assert out["x"].source == "Sleeper" and gw.report.line("Sleeper") == "projections: Sleeper"


def test_gateway_merges_expert_over_default(sleeper_index, ref):
    src = FantasyAPISource(FakeFP(), lambda r: sleeper_index)
    expert = src.week(ref, 5)
    some_id = next(iter(expert))
    default = FakeDefault({some_id: Projection(some_id, 5, {"rec": 99}), "only_default": Projection("only_default", 5, {"rec": 1})})
    gw = ProjectionGateway(default, src, "Sleeper")
    out = gw.week(ref, 5)
    assert out[some_id].source == "FantasyAPISource" and out[some_id].stats.get("rec") != 99
    assert out["only_default"].source == "Sleeper"
    assert gw.report.expert_players == len(expert) and gw.report.default_players == 1
    assert gw.report.line("Sleeper").startswith("projections: FantasyAPISource for")


def test_gateway_falls_back_when_expert_fails(sleeper_index, ref):
    src = FantasyAPISource(FakeFP(fail=True), lambda r: sleeper_index)
    default = FakeDefault({"x": Projection("x", 5, {"rec": 1})})
    gw = ProjectionGateway(default, src, "Sleeper")
    out = gw.week(ref, 5)
    assert out["x"].source == "Sleeper" and "429" in gw.report.error
    assert "failed" in gw.report.line("Sleeper")


def test_api_key_from_env_accepts_both_names(monkeypatch):
    from ffagent.sources.fantasy_api_source import api_key_from_env
    monkeypatch.delenv("FANTASY_INFORMATION_SOURCE_API_KEY", raising=False)
    monkeypatch.delenv("FANTASY_INFORMATION_SOURCE_KEY", raising=False)
    assert api_key_from_env() is None
    monkeypatch.setenv("FANTASY_INFORMATION_SOURCE_KEY", "k2")
    assert api_key_from_env() == "k2"
    monkeypatch.setenv("FANTASY_INFORMATION_SOURCE_API_KEY", "k1")
    assert api_key_from_env() == "k1"


def test_expert_projection_carries_fp_points_totals():
    ep = map_projection(PROJ["RB"][0])
    assert {"fp_points", "fp_points_ppr", "fp_points_half"} <= set(ep.stats)
    assert ep.stats["fp_points_ppr"] >= ep.stats["fp_points_half"] >= ep.stats["fp_points"]


def test_client_disk_cache_honours_ttl(tmp_path):
    from datetime import UTC, datetime, timedelta

    import httpx

    from ffagent.sources.fantasy_api_source import FantasyAPISourceClient

    hits = []
    def handler(request):
        hits.append(request.url.path)
        return httpx.Response(200, json={"players": [{"name": "X"}]})
    now = [datetime(2026, 10, 9, 12, 0, tzinfo=UTC)]
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x/nfl")
    c = FantasyAPISourceClient("k", http=http, cache_dir=tmp_path, cache_hours=6, clock=lambda: now[0])
    assert c.projections(2026, 5, "RB") == [{"name": "X"}] and len(hits) == 1
    # a second client (new process) within the TTL reads from disk
    c2 = FantasyAPISourceClient("k", http=http, cache_dir=tmp_path, cache_hours=6, clock=lambda: now[0] + timedelta(hours=5))
    assert c2.projections(2026, 5, "RB") == [{"name": "X"}] and len(hits) == 1 and c2.calls == 0
    # after the TTL it refetches
    c3 = FantasyAPISourceClient("k", http=http, cache_dir=tmp_path, cache_hours=6, clock=lambda: now[0] + timedelta(hours=7))
    c3.projections(2026, 5, "RB")
    assert len(hits) == 2 and c3.calls == 1
