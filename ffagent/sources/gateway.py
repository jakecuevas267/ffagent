"""Projection gateway: expert source first when configured, platform defaults otherwise, per player."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ffagent.sources.projections import Projection

log = logging.getLogger(__name__)


@dataclass
class SourceReport:
    expert: str | None = None       # name of the expert source used, if any
    expert_players: int = 0         # players whose projection came from the expert
    default_players: int = 0        # players filled from the platform default
    unresolved: list[str] = field(default_factory=list)
    error: str | None = None

    def line(self, default_name: str) -> str:
        if self.expert is None:
            return f"projections: {default_name}" + (f" ({self.error})" if self.error else "")
        s = f"projections: {self.expert} for {self.expert_players} players, {default_name} for {self.default_players}"
        if self.error:
            s += f"; {self.expert} failed: {self.error}"
        return s


class ProjectionGateway:
    def __init__(self, default, expert=None, default_name: str = "platform"):
        self.default = default      # .week(ref, week)
        self.expert = expert        # optional, same interface, plus .name and .unresolved
        self.default_name = default_name
        self.report = SourceReport()

    def week(self, ref, week: int) -> dict[str, Projection]:
        base = dict(self.default.week(ref, week))
        for p in base.values():
            p.source = p.source or self.default_name
        self.report = SourceReport()
        if self.expert is None:
            self.report.default_players = len(base)
            return base
        self.report.expert = self.expert.name
        try:
            expert = self.expert.week(ref, week)
        except Exception as e:  # noqa: BLE001 - network, auth, rate limit: never block the workflow
            log.warning("expert source %s failed, using %s: %s", self.expert.name, self.default_name, e)
            self.report.error = f"{type(e).__name__}: {e}"[:120]
            self.report.default_players = len(base)
            return base
        merged = base | expert
        self.report.expert_players = len(expert)
        self.report.default_players = len(merged) - len(expert)
        self.report.unresolved = list(getattr(self.expert, "unresolved", []))
        return merged
