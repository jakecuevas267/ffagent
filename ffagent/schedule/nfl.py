"""NFL week schedule: kickoffs, byes, lock state. Source: ESPN's public scoreboard feed."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# Platforms disagree on a few abbreviations. Canonical form here is Sleeper's.
_ALIASES = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def canon_team(abbr: str | None) -> str | None:
    if abbr is None:
        return None
    a = abbr.upper()
    return _ALIASES.get(a, a)


@dataclass(frozen=True)
class Game:
    id: str
    kickoff: datetime  # aware, UTC
    home: str
    away: str
    state: str  # pre | in | post

    @property
    def teams(self) -> set[str]:
        return {self.home, self.away}

    def started(self, now: datetime) -> bool:
        return now >= self.kickoff or self.state in ("in", "post")


@dataclass
class Slate:
    date: date
    games: list[Game]

    @property
    def first_kickoff(self) -> datetime:
        return min(g.kickoff for g in self.games)

    @property
    def teams(self) -> set[str]:
        return set().union(*(g.teams for g in self.games))


@dataclass
class WeekSchedule:
    season: int
    week: int
    games: list[Game]
    teams_on_bye: set[str] = field(default_factory=set)

    def game_for(self, team: str) -> Game | None:
        t = canon_team(team)
        return next((g for g in self.games if t in g.teams), None)

    @property
    def first_kickoff(self) -> datetime:
        return min(g.kickoff for g in self.games)

    @property
    def last_kickoff(self) -> datetime:
        return max(g.kickoff for g in self.games)

    def slates(self, tz: ZoneInfo) -> list[Slate]:
        by_day: dict[date, list[Game]] = {}
        for g in sorted(self.games, key=lambda g: g.kickoff):
            by_day.setdefault(g.kickoff.astimezone(tz).date(), []).append(g)
        return [Slate(d, gs) for d, gs in sorted(by_day.items())]

    def is_locked(self, team: str, now: datetime) -> bool:
        g = self.game_for(team)
        return g is not None and g.started(now)

    def teams_locked_at(self, now: datetime) -> set[str]:
        return set().union(*(g.teams for g in self.games if g.started(now))) if self.games else set()

    def games_between(self, start: datetime, end: datetime) -> list[Game]:
        return [g for g in self.games if start <= g.kickoff < end]


def parse_scoreboard(raw: dict) -> WeekSchedule:
    games = []
    for e in raw.get("events", []):
        c = e["competitions"][0]
        home = next(x for x in c["competitors"] if x["homeAway"] == "home")
        away = next(x for x in c["competitors"] if x["homeAway"] == "away")
        games.append(Game(
            id=str(e["id"]),
            kickoff=datetime.fromisoformat(e["date"]).astimezone(UTC),
            home=canon_team(home["team"]["abbreviation"]), away=canon_team(away["team"]["abbreviation"]),
            state=c["status"]["type"].get("state", "pre"),
        ))
    return WeekSchedule(
        season=int(raw["season"]["year"]), week=int(raw["week"]["number"]), games=games,
        teams_on_bye={canon_team(t["abbreviation"]) for t in raw.get("week", {}).get("teamsOnBye", [])},
    )


def fetch_week(season: int, week: int, http: httpx.Client | None = None) -> WeekSchedule:
    client = http or httpx.Client(timeout=20)
    r = client.get(SCOREBOARD, params={"dates": season, "seasontype": 2, "week": week})
    r.raise_for_status()
    return parse_scoreboard(r.json())
