from __future__ import annotations

from typing import Protocol

from ffagent.domain.models import (
    LeagueRef,
    LeagueSettings,
    Matchup,
    Player,
    RosterEntry,
    Team,
    Transaction,
)


class AuthExpired(Exception):
    """Credentials for a platform are missing or no longer valid; message says how to fix it."""


class LeagueProvider(Protocol):
    def settings(self, ref: LeagueRef) -> LeagueSettings: ...
    def teams(self, ref: LeagueRef) -> list[Team]: ...
    def lineups(self, ref: LeagueRef, week: int) -> dict[str, list[RosterEntry]]: ...
    def matchup(self, ref: LeagueRef, week: int) -> Matchup: ...
    def free_agents(self, ref: LeagueRef) -> list[Player]: ...
    def transactions(self, ref: LeagueRef, week: int) -> list[Transaction]: ...
    def players(self) -> list[Player]: ...
