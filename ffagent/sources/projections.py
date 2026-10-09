"""Weekly projections from Sleeper's (undocumented, public) projections endpoint.

Returns raw stat lines so points are computed under each league's own scoring table.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

PROJECTIONS = "https://api.sleeper.app/projections/nfl/{season}/{week}"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


@dataclass
class Projection:
    player_id: str
    week: int
    stats: dict[str, float] = field(default_factory=dict)
    team: str | None = None
    opponent: str | None = None
    source: str | None = None


def score(stats: dict[str, float], scoring: dict[str, float]) -> float:
    """Points under a league's scoring table. Stat keys and scoring keys share Sleeper's namespace."""
    return sum(float(stats[k]) * float(v) for k, v in scoring.items() if k in stats)


def parse_projections(raw: list[dict]) -> dict[str, Projection]:
    out: dict[str, Projection] = {}
    for p in raw:
        pid = p.get("player_id")
        if not pid or not p.get("stats"):
            continue
        out[str(pid)] = Projection(
            player_id=str(pid), week=int(p["week"]), stats={k: float(v) for k, v in p["stats"].items()},
            team=p.get("team"), opponent=p.get("opponent"),
        )
    return out


class SleeperProjections:
    def __init__(self, http: httpx.Client | None = None):
        self._http = http or httpx.Client(timeout=60)
        self._cache: dict[tuple[int, int], dict[str, Projection]] = {}

    def week(self, ref, week: int) -> dict[str, Projection]:
        season = ref.season
        key = (season, week)
        if key not in self._cache:
            params = [("season_type", "regular")] + [("position[]", p) for p in POSITIONS]
            r = self._http.get(PROJECTIONS.format(season=season, week=week), params=params)
            r.raise_for_status()
            self._cache[key] = parse_projections(r.json())
        return self._cache[key]
