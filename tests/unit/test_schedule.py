import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ffagent.schedule.nfl import WeekSchedule, parse_scoreboard

FIX = Path(__file__).parent.parent / "fixtures" / "espn" / "scoreboard_2026_w5.json"
DEN = ZoneInfo("America/Denver")


@pytest.fixture
def week() -> WeekSchedule:
    return parse_scoreboard(json.loads(FIX.read_text()))


def test_parse_basics(week):
    assert week.week == 5 and week.season == 2026
    assert len(week.games) == 15
    assert week.teams_on_bye == {"KC", "CAR"}


def test_game_lookup_by_team(week):
    g = week.game_for("DAL")
    assert g is not None and {g.home, g.away} == {"TB", "DAL"}
    assert g.kickoff == datetime(2026, 10, 9, 0, 15, tzinfo=UTC)
    assert week.game_for("KC") is None          # bye
    assert week.game_for("WSH") is not None     # ESPN spells Washington WSH


def test_sleeper_team_abbreviations_are_accepted(week):
    # Sleeper uses WAS; ESPN uses WSH. The schedule must answer for both.
    assert week.game_for("WAS") is week.game_for("WSH")


def test_slates_group_games_by_local_date(week):
    slates = week.slates(DEN)
    assert [s.date.isoformat() for s in slates] == ["2026-10-08", "2026-10-11", "2026-10-12"]
    assert [len(s.games) for s in slates] == [1, 13, 1]
    assert slates[0].first_kickoff == datetime(2026, 10, 9, 0, 15, tzinfo=UTC)


def test_first_and_last_kickoff(week):
    assert week.first_kickoff == datetime(2026, 10, 9, 0, 15, tzinfo=UTC)
    assert week.last_kickoff == datetime(2026, 10, 13, 0, 15, tzinfo=UTC)


class TestLocking:
    def test_before_kickoff_not_locked(self, week):
        now = datetime(2026, 10, 8, 23, 0, tzinfo=UTC)
        assert not week.is_locked("DAL", now)

    def test_at_kickoff_locked(self, week):
        now = datetime(2026, 10, 9, 0, 15, tzinfo=UTC)
        assert week.is_locked("DAL", now) and week.is_locked("TB", now)
        assert not week.is_locked("BUF", now)

    def test_bye_team_is_never_lockable_but_has_no_game(self, week):
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        assert not week.is_locked("KC", now)
        assert week.game_for("KC") is None

    def test_teams_locked_at(self, week):
        now = week.first_kickoff + timedelta(hours=1)
        assert week.teams_locked_at(now) == {"DAL", "TB"}


def test_games_in_window(week):
    start = datetime(2026, 10, 11, 16, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    early = week.games_between(start, end)
    assert len(early) == 8 and all(g.kickoff.hour == 17 for g in early)
