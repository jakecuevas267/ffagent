"""Player identity: canonical ids, cross-platform ids, and name resolution that never guesses."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from .models import Platform, Player, Position

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_PUNCT = re.compile(r"[^a-z0-9 ]")


def normalize_name(name: str) -> str:
    s = _PUNCT.sub("", name.lower())
    parts = [p for p in s.split() if p]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


@dataclass
class Resolution:
    status: Literal["exact", "ambiguous", "unresolved"]
    player: Player | None = None
    candidates: list[Player] = field(default_factory=list)


class PlayerIndex:
    def __init__(self, players: list[Player]):
        self._by_id: dict[str, Player] = {}
        self._by_ext: dict[tuple[Platform, str], Player] = {}
        self._by_name: dict[str, list[Player]] = {}
        for p in players:
            self._by_id[p.id] = p
            for plat, ext in p.external_ids.items():
                self._by_ext[(plat, str(ext))] = p
            self._by_name.setdefault(normalize_name(p.name), []).append(p)

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self):
        return iter(self._by_id.values())

    def by_id(self, player_id: str) -> Player | None:
        return self._by_id.get(player_id)

    def by_external(self, platform: Platform, ext_id: str) -> Player | None:
        if platform is Platform.SLEEPER:
            return self.by_id(ext_id)
        return self._by_ext.get((platform, str(ext_id)))

    def resolve(self, name: str, position: Position, team: str | None = None) -> Resolution:
        """Resolve an outside name (e.g. from a rankings sheet) to a player.

        Exact only when precisely one player matches name + position (+ team when given).
        Any other outcome is reported, not guessed.
        """
        candidates = [p for p in self._by_name.get(normalize_name(name), []) if position in p.positions]
        if not candidates:
            return Resolution("unresolved")
        if team is not None:
            on_team = [p for p in candidates if p.team == team]
            if len(on_team) == 1:
                return Resolution("exact", on_team[0], on_team)
            return Resolution("ambiguous", None, candidates)
        if len(candidates) == 1:
            return Resolution("exact", candidates[0], candidates)
        return Resolution("ambiguous", None, candidates)
