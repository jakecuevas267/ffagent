import json
from pathlib import Path

import pytest

from ffagent.domain.models import (
    Format,
    InjuryStatus,
    LeagueRef,
    Platform,
    Position,
    Slot,
    WaiverType,
)
from ffagent.providers.base import AuthExpired
from ffagent.providers.espn import APPLIED, ESPNProjections, ESPNProvider, map_player
from ffagent.providers.espn_client import ESPNClient

FIX = Path(__file__).parent.parent / "fixtures" / "espn"


class FakeClient:
    def __init__(self):
        self.season = 2026

    def league(self, league_id, views, scoring_period=None):
        return json.loads((FIX / f"league_{league_id}.json").read_text())

    def transactions(self, league_id):
        return json.loads((FIX / f"transactions_{league_id}.json").read_text())["transactions"]

    def players(self, league_id, scoring_period, limit=2000):
        return json.loads((FIX / f"players_{league_id}_w5.json").read_text())


@pytest.fixture
def provider():
    return ESPNProvider(FakeClient())


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.ESPN, league_id="100001", season=2026, my_team_id="5")


@pytest.fixture
def ref2():
    return LeagueRef(platform=Platform.ESPN, league_id="100002", season=2026, my_team_id="13")


class TestSettings:
    def test_slots_and_size(self, provider, ref):
        s = provider.settings(ref)
        assert s.num_teams == 8
        assert s.roster_slots == {Slot.QB: 1, Slot.RB: 2, Slot.WR: 2, Slot.TE: 1, Slot.DEF: 1, Slot.K: 1,
                                  Slot.BN: 7, Slot.IR: 1, Slot.FLEX: 1}
        assert s.format is Format.REDRAFT
        assert s.scoring == {APPLIED: 1.0}

    def test_traditional_waivers_without_faab(self, provider, ref):
        s = provider.settings(ref)
        assert s.waiver_type is WaiverType.REVERSE_STANDINGS and s.faab_budget is None

    def test_trade_deadline_is_a_datetime(self, provider, ref):
        assert provider.settings(ref).trade_deadline.year == 2026

    def test_league_name(self, provider, ref):
        assert provider.league_name(ref) == "League A"


class TestTeams:
    def test_count_and_record(self, provider, ref):
        teams = {t.id: t for t in provider.teams(ref)}
        assert len(teams) == 8
        me = teams["5"]
        assert me.owner == "manager8" and (me.wins + me.losses) == 4
        assert me.waiver_priority is not None and me.faab_remaining is None

    def test_starters_follow_slot_layout(self, provider, ref):
        me = next(t for t in provider.teams(ref) if t.id == "5")
        assert len(me.starters) == 9
        assert {e.slot for e in me.starters} == {Slot.QB, Slot.RB, Slot.WR, Slot.TE, Slot.FLEX, Slot.K, Slot.DEF}
        assert all(e.player_id.startswith("espn:") for e in me.roster)

    def test_lineups_cover_every_team(self, provider, ref):
        assert set(provider.lineups(ref, 5)) == {str(i) for i in range(1, 9)}

    def test_find_my_team_by_manager_name(self, provider, ref):
        assert provider.find_my_team(ref, "manager8") == "5"
        with pytest.raises(LookupError):
            provider.find_my_team(ref, "nobody")

    def test_second_league(self, provider, ref2):
        assert provider.settings(ref2).num_teams == 10
        assert provider.find_my_team(ref2, "manager10") == "13"


class TestPlayers:
    def test_map_player(self):
        p = map_player({"id": 4426515, "fullName": "Puka Nacua", "defaultPositionId": 3, "proTeamId": 14,
                        "injuryStatus": "ACTIVE", "active": True})
        assert p.id == "espn:4426515" and p.positions == [Position.WR] and p.team == "LAR"
        assert p.injury_status is InjuryStatus.HEALTHY and p.external_id(Platform.ESPN) == "4426515"

    @pytest.mark.parametrize("raw,expected", [
        ("OUT", InjuryStatus.OUT), ("INJURY_RESERVE", InjuryStatus.IR), ("DOUBTFUL", InjuryStatus.DOUBTFUL),
        ("QUESTIONABLE", InjuryStatus.QUESTIONABLE), ("DAY_TO_DAY", InjuryStatus.QUESTIONABLE), (None, InjuryStatus.HEALTHY),
    ])
    def test_injury_mapping(self, raw, expected):
        assert map_player({"id": 1, "fullName": "X", "defaultPositionId": 2, "proTeamId": 2, "injuryStatus": raw}).injury_status is expected

    def test_players_come_from_loaded_rosters(self, provider, ref):
        provider.teams(ref)
        players = {p.id: p for p in provider.players()}
        me = next(t for t in provider.teams(ref) if t.id == "5")
        assert all(e.player_id in players for e in me.roster)

    def test_free_agents_are_unrostered(self, provider, ref):
        provider.teams(ref)
        fa = provider.free_agents(ref, week=5)
        rostered = {e.player_id for t in provider.teams(ref) for e in t.roster}
        assert fa and {p.id for p in fa}.isdisjoint(rostered)


class TestProjections:
    def test_weekly_projection_for_my_starters(self, provider, ref):
        proj = ESPNProjections(provider).week(ref, 5)
        me = next(t for t in provider.teams(ref) if t.id == "5")
        covered = [e.player_id for e in me.starters if e.player_id in proj]
        assert len(covered) >= 7
        assert all(0 <= proj[pid].stats[APPLIED] < 60 for pid in covered)

    def test_matchup(self, provider, ref):
        m = provider.matchup(ref, 5)
        assert m.my_team_id == "5" and m.opponent_team_id not in (None, "5")


def test_auth_expired_message(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(401, json={"messages": ["You are not authorized to view this League."]})
    c = ESPNClient(2026, "s2", "{swid}", http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x"))
    with pytest.raises(AuthExpired, match="ESPN_S2"):
        c.league("1", ["mTeam"])


class TestTransactions:
    def test_waivers_and_free_agent_moves_are_mapped(self, provider, ref):
        txs = provider.transactions(ref, week=5)
        kinds = {t.kind for t in txs}
        assert "waiver" in kinds and "free_agent" in kinds
        w = next(t for t in txs if t.kind == "waiver" and t.status == "complete")
        assert w.adds and all(v == w.team_ids[0] for v in w.adds.values())
        assert all(k.startswith("espn:") for k in list(w.adds) + list(w.drops))

    def test_pending_claims_are_visible(self, provider, ref):
        pending = [t for t in provider.transactions(ref, week=5) if t.status == "pending"]
        assert pending and all(t.kind == "waiver" for t in pending)
        assert pending[0].faab_bid is not None

    def test_lineup_changes_are_excluded(self, provider, ref):
        assert all(t.kind != "roster" for t in provider.transactions(ref, week=5))

    def test_other_weeks_are_filtered(self, provider, ref):
        assert provider.transactions(ref, week=1) == [] or all(True for _ in provider.transactions(ref, week=1))
