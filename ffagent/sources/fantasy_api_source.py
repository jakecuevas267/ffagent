"""A commercial fantasy data API (v2 JSON) as an expert source. Key: FANTASY_INFORMATION_SOURCE_API_KEY (or _KEY).

Free tier: every response is capped at 10 players and calls are rate-limited, so coverage is the
top of each position; the gateway fills the rest from the platform's own projections.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx

from ffagent.domain.identity import PlayerIndex, normalize_name
from ffagent.domain.models import Position
from ffagent.sources.projections import Projection

URL_VAR = "FANTASY_INFORMATION_SOURCE_URL"  # base URL of the data API, e.g. https://<host>/public/v2/json/nfl
KEY_VARS = ("FANTASY_INFORMATION_SOURCE_API_KEY", "FANTASY_INFORMATION_SOURCE_KEY")
POSITIONS = {"QB": Position.QB, "RB": Position.RB, "WR": Position.WR, "TE": Position.TE, "K": Position.K, "DST": Position.DEF}
# Source stat names -> Sleeper scoring keys, so league scoring tables apply directly.
STAT_MAP = {"pass_yds": "pass_yd", "pass_tds": "pass_td", "pass_ints": "pass_int", "pass_att": "pass_att", "pass_cmp": "pass_cmp",
            "rush_yds": "rush_yd", "rush_tds": "rush_td", "rush_att": "rush_att",
            "rec_rec": "rec", "rec_yds": "rec_yd", "rec_tds": "rec_td", "fumbles": "fum_lost", "ret_tds": "st_td",
            "pass_yds_300": "bonus_pass_yd_300", "pass_yds_400": "bonus_pass_yd_400",
            "rush_yds_100": "bonus_rush_yd_100", "rush_yds_200": "bonus_rush_yd_200",
            "rec_yds_100": "bonus_rec_yd_100", "rec_yds_200": "bonus_rec_yd_200"}
SCORING = {"ppr": "PPR", "half": "HALF", "std": "STD"}


def api_key_from_env() -> str | None:
    for var in KEY_VARS:
        if os.environ.get(var):
            return os.environ[var]
    return None


class FantasyAPISourceClient:
    def __init__(self, api_key: str, base_url: str | None = None, http: httpx.Client | None = None):
        base_url = base_url or os.environ.get(URL_VAR)
        if http is None and not base_url:
            raise RuntimeError(f"{URL_VAR} is not set; it must hold the data API base URL")
        self._http = http or httpx.Client(base_url=base_url, timeout=60, headers={"x-api-key": api_key})
        self._cache: dict[tuple, dict] = {}

    def _get(self, path: str, **params) -> dict:
        key = (path, tuple(sorted(params.items())))
        if key not in self._cache:
            r = self._http.get(path, params=params)
            r.raise_for_status()
            self._cache[key] = r.json()
        return self._cache[key]

    def projections(self, season: int, week: int, position: str, scoring: str = "PPR") -> list[dict]:
        return self._get(f"/{season}/projections", position=position, scoring=scoring, week=week).get("players", [])

    def rankings(self, season: int, position: str, scoring: str = "PPR", week: int | None = None) -> list[dict]:
        kind = "weekly" if week else "ROS"
        params = {"position": position, "scoring": scoring, "type": kind}
        if week:
            params["week"] = week
        return self._get(f"/{season}/consensus-rankings", **params).get("players", [])

    def injuries(self, season: int, week: int) -> list[dict]:
        return self._get("/injuries", season=season, week=week).get("injuries", [])


@dataclass
class ExpertProjection:
    name: str
    position: Position
    team: str | None
    stats: dict[str, float] = field(default_factory=dict)
    yahoo_id: str | None = None
    rank: int | None = None
    tier: int | None = None


def map_projection(raw: dict) -> ExpertProjection | None:
    pos = POSITIONS.get(raw.get("position_id"))
    if pos is None:
        return None
    stats = {STAT_MAP[k]: float(v) for k, v in raw.get("stats", {}).items() if k in STAT_MAP}
    for k in ("points", "points_ppr", "points_half"):
        if k in raw.get("stats", {}):
            stats[f"fp_{k}"] = float(raw["stats"][k])
    return ExpertProjection(name=raw["name"], position=pos, team=raw.get("team_id"), stats=stats)


def resolve(ep: ExpertProjection, index: PlayerIndex):
    """Match an expert row to a platform player: Yahoo id first (exact), then name + position + team."""
    if ep.yahoo_id:
        for p in index:
            if p.external_ids.get("yahoo") == str(ep.yahoo_id):
                return p
    r = index.resolve(ep.name, ep.position, ep.team)
    if r.status == "exact":
        return r.player
    if ep.position is Position.DEF and ep.team:   # "Buffalo Bills" vs "Bills": match defenses by team
        return next((p for p in index if Position.DEF in p.positions and p.team == ep.team), None)
    if r.status == "ambiguous" and len(r.candidates) == 1 and normalize_name(r.candidates[0].name) == normalize_name(ep.name):
        return r.candidates[0]  # same name, team disagrees (traded, stale team): accept the only candidate
    return None


class FantasyAPISource:
    """Expert projections keyed by the platform's canonical player ids. Skill positions only:
    K and DST projections use stat names the league scoring cannot apply, so those fall through."""

    name = "FantasyAPISource"
    positions = ("QB", "RB", "WR", "TE")

    def __init__(self, client: FantasyAPISourceClient, index_for_ref):
        self._c = client
        self._index_for_ref = index_for_ref  # (ref) -> PlayerIndex of that platform's players
        self.unresolved: list[str] = []

    def week(self, ref, week: int) -> dict[str, Projection]:
        index = self._index_for_ref(ref)
        out: dict[str, Projection] = {}
        self.unresolved = []
        for pos in self.positions:
            for raw in self._c.projections(ref.season, week, pos):
                ep = map_projection(raw)
                if ep is None:
                    continue
                p = resolve(ep, index)
                if p is None:
                    self.unresolved.append(f"{ep.name} ({ep.position}, {ep.team})")
                    continue
                out[p.id] = Projection(player_id=p.id, week=week, stats=ep.stats, team=p.team, source=self.name)
        return out
