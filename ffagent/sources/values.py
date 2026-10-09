"""Rest-of-season value per player, on one scale: points per game under the league's scoring.

Order: expert season projections (stat lines scored per league, divided by games) → platform
next-week projection. Reports which source produced each value.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ffagent.sources.projections import score

log = logging.getLogger(__name__)
SEASON_GAMES = 17


@dataclass
class Value:
    player_id: str
    ppg: float
    source: str


class ValueGateway:
    def __init__(self, default_projections, expert=None, default_name: str = "platform"):
        self.default = default_projections   # .week(ref, week) -> dict[player_id, Projection]
        self.expert = expert                 # optional: .season(ref) -> dict[player_id, Projection] (season totals)
        self.default_name = default_name
        self.report: dict = {}

    def values(self, ref, next_week: int, scoring: dict[str, float]) -> dict[str, Value]:
        out: dict[str, Value] = {}
        for pid, proj in self.default.week(ref, next_week).items():
            out[pid] = Value(pid, round(score(proj.stats, scoring), 2), f"{self.default_name} week {next_week}")
        self.report = {"expert": None, "expert_players": 0, "default_players": len(out), "error": None}
        if self.expert is None:
            return out
        self.report["expert"] = self.expert.name
        try:
            season = self.expert.season(ref)
        except Exception as e:  # noqa: BLE001 - never block the workflow on the expert source
            log.warning("expert values failed, using %s: %s", self.default_name, e)
            self.report["error"] = f"{type(e).__name__}: {e}"[:120]
            return out
        for pid, proj in season.items():
            out[pid] = Value(pid, round(score(proj.stats, scoring) / SEASON_GAMES, 2), f"{self.expert.name} season")
        self.report["expert_players"] = len(season)
        self.report["default_players"] = len(out) - len(season)
        return out

    def line(self) -> str:
        r = self.report
        if not r.get("expert"):
            return f"values: {self.default_name} next-week projections" + (f" ({r['error']})" if r.get("error") else "")
        s = f"values: {r['expert']} season projections for {r['expert_players']} players, {self.default_name} next-week for {r['default_players']}"
        if r.get("error"):
            s += f"; {r['expert']} failed: {r['error']}"
        return s
