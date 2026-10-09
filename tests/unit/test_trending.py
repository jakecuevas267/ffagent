from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueRef, Platform, Player
from ffagent.providers.sleeper import SleeperProvider
from ffagent.sources.trending import SleeperTrends
from tests.providers.test_sleeper import FakeClient


def test_sleeper_league_gets_raw_trends():
    provider = SleeperProvider(FakeClient())
    ref = LeagueRef(platform=Platform.SLEEPER, league_id="1", season=2026)
    t = SleeperTrends(provider).for_league("add", ref, PlayerIndex(provider.players()))
    assert len(t) == 25 and t[0].count >= t[-1].count and "adds in 24h" in t[0].label


def test_espn_league_gets_trends_mapped_through_espn_ids():
    provider = SleeperProvider(FakeClient())
    sleeper_players = provider.players()
    espn_players = [Player(id=f"espn:{p.external_id(Platform.ESPN)}", name=p.name, positions=p.positions, team=p.team,
                           external_ids={Platform.ESPN: p.external_id(Platform.ESPN)})
                    for p in sleeper_players if p.external_id(Platform.ESPN)]
    ref = LeagueRef(platform=Platform.ESPN, league_id="1", season=2026)
    trends = SleeperTrends(provider).for_league("add", ref, PlayerIndex(espn_players))
    sidx = PlayerIndex(sleeper_players)
    mappable = [t for t in provider.trending("add") if (p := sidx.by_id(t.player_id)) and p.external_id(Platform.ESPN)]
    assert len(trends) == len(mappable) and all(t.player_id.startswith("espn:") for t in trends)
    assert trends[0].count == mappable[0].count


def test_espn_fallback_by_name_when_no_espn_id():
    provider = SleeperProvider(FakeClient())
    sidx = PlayerIndex(provider.players())
    sp = next(sidx.by_id(t.player_id) for t in provider.trending("add") if sidx.by_id(t.player_id))
    espn_only = PlayerIndex([Player(id="espn:999", name=sp.name, positions=sp.positions, team=sp.team)])
    ref = LeagueRef(platform=Platform.ESPN, league_id="1", season=2026)
    t = SleeperTrends(provider).for_league("add", ref, espn_only)
    assert t and t[0].player_id == "espn:999"
