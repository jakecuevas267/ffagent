"""ESPN fantasy v3: unofficial, undocumented endpoints. Private leagues need the espn_s2 + SWID cookies."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from ffagent.providers.base import AuthExpired

READS = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
_HOW_TO = ("set ESPN_S2 and ESPN_SWID in .env (fantasy.espn.com → DevTools → Application → Cookies); "
           "they expire periodically and must be refreshed")


class ESPNClient:
    def __init__(self, season: int, espn_s2: str | None, swid: str | None, http: httpx.Client | None = None):
        self.season = season
        cookies = {k: v for k, v in {"espn_s2": espn_s2, "SWID": swid}.items() if v}
        self._http = http or httpx.Client(base_url=READS, timeout=30, cookies=cookies,
                                          headers={"User-Agent": "Mozilla/5.0 (ffagent)"})

    @classmethod
    def from_env(cls, season: int) -> ESPNClient:
        return cls(season, os.environ.get("ESPN_S2"), os.environ.get("ESPN_SWID"))

    def _get(self, path: str, params: list[tuple[str, Any]] | None = None, filter: dict | None = None) -> Any:
        headers = {"X-Fantasy-Filter": json.dumps(filter)} if filter else None
        r = self._http.get(path, params=params, headers=headers)
        if r.status_code in (401, 403):
            raise AuthExpired(f"ESPN rejected the request ({r.status_code}); {_HOW_TO}")
        r.raise_for_status()
        return r.json()

    def league(self, league_id: str, views: list[str], scoring_period: int | None = None) -> dict:
        params: list[tuple[str, Any]] = [("view", v) for v in views]
        if scoring_period is not None:
            params.append(("scoringPeriodId", scoring_period))
        return self._get(f"/seasons/{self.season}/segments/0/leagues/{league_id}", params)

    def transactions(self, league_id: str) -> list[dict]:
        return self._get(f"/seasons/{self.season}/segments/0/leagues/{league_id}", [("view", "mTransactions2")]).get("transactions", [])

    def players(self, league_id: str, scoring_period: int, limit: int = 2000) -> list[dict]:
        """Player pool incl. free agents, with ESPN's weekly projections. Filtered to fantasy positions."""
        flt = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                           "filterSlotIds": {"value": [0, 2, 4, 6, 16, 17, 23]},
                           "limit": limit, "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}
        return self._get(f"/seasons/{self.season}/segments/0/leagues/{league_id}",
                         [("view", "kona_player_info"), ("scoringPeriodId", scoring_period)], flt).get("players", [])
