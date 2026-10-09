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
from ffagent.providers.sleeper import SleeperProvider, map_players

FIX = Path(__file__).parent.parent / "fixtures" / "sleeper"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


class FakeClient:
    """Serves recorded JSON instead of hitting api.sleeper.app."""

    def __init__(self):
        self.calls = []

    def _get(self, name):
        self.calls.append(name)
        return load(name)

    def league(self, league_id):          return self._get("league")
    def rosters(self, league_id):         return self._get("rosters")
    def users(self, league_id):           return self._get("users")
    def matchups(self, league_id, week):  return self._get("matchups_w5")
    def transactions(self, league_id, week): return self._get("transactions_w5")
    def players(self):                    return self._get("players")
    def state(self):                      return self._get("state")
    def trending(self, kind="add", lookback_hours=24, limit=25): return self._get(f"trending_{kind}")


@pytest.fixture
def ref():
    return LeagueRef(platform=Platform.SLEEPER, league_id="1000000000000000001",
                     season=2026, my_team_id="1", name="Test League")


@pytest.fixture
def provider():
    return SleeperProvider(FakeClient())


class TestSettings:
    def test_roster_slots_and_format(self, provider, ref):
        s = provider.settings(ref)
        assert s.num_teams == 10
        assert s.roster_slots == {Slot.QB: 1, Slot.RB: 2, Slot.WR: 2, Slot.TE: 1,
                                  Slot.FLEX: 1, Slot.K: 1, Slot.DEF: 1, Slot.BN: 6, Slot.IR: 1}
        assert s.format is Format.REDRAFT
        assert s.scoring["rec"] == 0.5
        assert s.scoring["pass_td"] == 4

    def test_faab_waivers(self, provider, ref):
        s = provider.settings(ref)
        assert s.waiver_type is WaiverType.FAAB
        assert s.faab_budget == 100
        assert s.trade_deadline_week == 12


class TestTeams:
    def test_teams_have_owner_names_and_records(self, provider, ref):
        teams = {t.id: t for t in provider.teams(ref)}
        assert len(teams) == 10
        me = teams["1"]
        assert me.owner == "alice"
        assert me.name == "Alice's Team"
        assert (me.wins, me.losses) == (3, 1)
        assert me.faab_remaining == 100 - 23

    def test_starters_are_mapped_to_slots_in_order(self, provider, ref):
        me = next(t for t in provider.teams(ref) if t.id == "1")
        assert [e.slot for e in me.starters] == [Slot.QB, Slot.RB, Slot.RB, Slot.WR, Slot.WR,
                                                 Slot.TE, Slot.FLEX, Slot.K, Slot.DEF]
        assert me.starters[0].player_id == "4984"       # Josh Allen
        assert [e.player_id for e in me.bench] == ["6790", "8138", "9509", "11631", "4046", "7547"]

    def test_empty_starter_slot(self, provider, ref):
        # Sleeper encodes an empty slot as player id "0".
        t = next(t for t in provider.teams(ref) if t.id == "2")
        assert t.starters[-1].is_empty and t.starters[-1].slot is Slot.DEF

    def test_ir_slot(self, provider, ref):
        me = next(t for t in provider.teams(ref) if t.id == "1")
        ir = [e for e in me.roster if e.slot is Slot.IR]
        assert [e.player_id for e in ir] == ["5872"]

    def test_lineups_returns_every_team(self, provider, ref):
        lineups = provider.lineups(ref, week=5)
        assert set(lineups) == {str(i) for i in range(1, 11)}
        assert lineups["1"][0].player_id == "4984"


class TestPlayers:
    def test_map_players_builds_domain_players(self):
        players = map_players(load("players"))
        swift = next(p for p in players if p.id == "6790")
        assert swift.name == "D'Andre Swift"
        assert swift.positions == [Position.RB]
        assert swift.team == "CHI"
        assert swift.injury_status is InjuryStatus.QUESTIONABLE
        assert swift.external_id(Platform.ESPN) == "4259545"

    def test_team_defenses_are_players(self):
        players = map_players(load("players"))
        buf = next(p for p in players if p.id == "BUF")
        assert buf.positions == [Position.DEF] and buf.team == "BUF"

    def test_inactive_and_non_fantasy_players_are_dropped(self):
        players = map_players(load("players"))
        assert not any(p.id == "retired1" for p in players)
        assert not any(p.id == "ol1" for p in players)

    def test_free_agents_are_everyone_not_rostered(self, provider, ref):
        fa = {p.id for p in provider.free_agents(ref)}
        rostered = {e.player_id for t in provider.teams(ref) for e in t.roster if e.player_id}
        assert fa.isdisjoint(rostered)
        assert "11632" in fa     # a known free agent in the fixture
        assert "4984" not in fa  # Josh Allen is rostered


class TestMatchups:
    def test_my_matchup(self, provider, ref):
        m = provider.matchup(ref, week=5)
        assert m.week == 5
        assert m.my_team_id == "1" and m.opponent_team_id == "4"
        assert m.my_points == pytest.approx(0.0)


class TestTransactions:
    def test_waiver_claims_and_trades(self, provider, ref):
        txs = provider.transactions(ref, week=5)
        kinds = [t.kind for t in txs]
        assert "waiver" in kinds and "trade" in kinds and "free_agent" in kinds
        w = next(t for t in txs if t.kind == "waiver")
        assert w.adds == {"5859": "3"} and w.drops == {"3214": "3"} and w.faab_bid == 12
        assert w.status == "complete"


class TestCurrentWeek:
    def test_state(self, provider):
        assert provider.current_week() == 5
        assert provider.current_season() == 2026


class TestFindMyTeam:
    def test_by_display_name(self, provider, ref):
        assert provider.find_my_team(ref, "alice") == "1"

    def test_case_insensitive(self, provider, ref):
        assert provider.find_my_team(ref, "ALICE") == "1"

    def test_unknown_user(self, provider, ref):
        with pytest.raises(LookupError):
            provider.find_my_team(ref, "nobody")

    def test_co_owner(self, provider, ref):
        assert provider.find_my_team(ref, "bob") == "2"

    def test_commissioner_without_roster_is_none(self, provider, ref):
        assert provider.find_my_team(ref, "commish") is None


class TestIRRules:
    def test_ir_slots_come_from_settings_not_roster_positions(self, provider, ref):
        s = provider.settings(ref)
        assert s.roster_slots[Slot.IR] == 1 and Slot.TAXI not in s.roster_slots

    def test_ir_statuses_follow_league_flags(self, provider, ref):
        s = provider.settings(ref)  # fixture: reserve_allow_out=1, nothing else
        assert s.ir_eligible(InjuryStatus.IR) and s.ir_eligible(InjuryStatus.OUT)
        assert not s.ir_eligible(InjuryStatus.DOUBTFUL) and not s.ir_eligible(InjuryStatus.QUESTIONABLE)


class TestTrending:
    def test_trending_adds(self, provider):
        t = provider.trending("add")
        assert len(t) == 25 and t[0][1] >= t[-1][1]
        assert all(isinstance(pid, str) and isinstance(n, int) for pid, n in t)

    def test_trending_drops(self, provider):
        assert provider.trending("drop")
