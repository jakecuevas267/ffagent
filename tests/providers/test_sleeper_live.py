"""Opt-in contract tests against the real Sleeper API: `pytest -m live`.

They assert shapes, not values, so they catch upstream changes without being brittle.
"""
import os

import pytest

from ffagent.domain.models import LeagueRef, Platform
from ffagent.providers.sleeper import SleeperClient, SleeperProvider

pytestmark = pytest.mark.live
USER = os.environ.get("FFAGENT_LIVE_SLEEPER_USER")


@pytest.fixture(scope="module")
def provider():
    return SleeperProvider(SleeperClient())


@pytest.fixture(scope="module")
def ref(provider):
    if not USER:
        pytest.skip("set FFAGENT_LIVE_SLEEPER_USER")
    season = provider.current_season()
    user = provider._c.user(USER)
    leagues = provider._c.user_leagues(user["user_id"], season)
    if not leagues:
        pytest.skip(f"{USER} has no {season} leagues")
    ref = LeagueRef(platform=Platform.SLEEPER, league_id=leagues[0]["league_id"], season=season)
    ref.my_team_id = provider.find_my_team(ref, USER)
    return ref


def test_state(provider):
    assert 1 <= provider.current_week() <= 18


def test_settings_and_teams(provider, ref):
    s = provider.settings(ref)
    teams = provider.teams(ref)
    assert s.num_teams == len(teams)
    me = next(t for t in teams if t.id == ref.my_team_id)
    assert len(me.starters) == len(s.starting_slots)


def test_lineups_cover_every_team(provider, ref):
    week = provider.current_week()
    assert set(provider.lineups(ref, week)) == {t.id for t in provider.teams(ref)}


def test_players_and_free_agents(provider, ref):
    players = provider.players()
    assert len(players) > 1000
    fa = provider.free_agents(ref)
    assert 0 < len(fa) < len(players)
