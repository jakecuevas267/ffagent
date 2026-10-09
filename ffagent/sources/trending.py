"""Sleeper's trending adds/drops as the pickup-trend signal for every league.

ESPN leagues get the same counts: Sleeper ids are mapped through the espn_id crosswalk in Sleeper's
player database, with name/position/team matching as the fallback.
"""
from __future__ import annotations

from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import Platform, Trend


class SleeperTrends:
    def __init__(self, sleeper_provider, lookback_hours: int = 24, limit: int = 25):
        self._sleeper = sleeper_provider
        self._lookback = lookback_hours
        self._limit = limit

    def _raw(self, kind: str) -> list[Trend]:
        return self._sleeper.trending(kind, lookback_hours=self._lookback, limit=self._limit)

    def for_league(self, kind: str, ref, index: PlayerIndex) -> list[Trend]:
        """Trends keyed by the league platform's player ids; unmappable players are dropped."""
        trends = self._raw(kind)
        if ref.platform is Platform.SLEEPER:
            return trends
        sleeper_index = PlayerIndex(self._sleeper.players())
        out = []
        for t in trends:
            sp = sleeper_index.by_id(t.player_id)
            if sp is None:
                continue
            target = None
            espn_id = sp.external_id(Platform.ESPN)
            if espn_id:
                target = index.by_external(Platform.ESPN, espn_id)
            if target is None:
                r = index.resolve(sp.name, sp.position, sp.team)
                target = r.player if r.status == "exact" else None
            if target is not None:
                out.append(Trend(player_id=target.id, count=t.count, label=t.label))
        return out
