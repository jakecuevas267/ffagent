"""League-wide lineup audit: starters who cannot play, and the obvious fix. Deterministic."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import InjuryStatus, Player, RosterEntry, Slot, Team
from ffagent.schedule.nfl import WeekSchedule

HARD = {InjuryStatus.OUT, InjuryStatus.IR, InjuryStatus.PUP, InjuryStatus.SUSPENDED, InjuryStatus.NA}


@dataclass
class Issue:
    slot: Slot
    kind: str                 # bye | out | doubtful | empty
    player_id: str | None
    player_name: str
    detail: str               # "on bye", "Out (knee)", "Doubtful (hamstring)", "empty slot"
    fixable: bool             # game not started (or empty slot) and a replacement exists or the slot can be filled
    fix: str | None           # suggested bench player name, if any
    locked: bool              # the starter's game already started

    @property
    def key(self) -> str:
        return f"{self.slot}:{self.player_id or 'empty'}:{self.kind}"


def _available_bench(team: Team, index: PlayerIndex, schedule: WeekSchedule, now: datetime) -> list[Player]:
    out = []
    for e in team.roster:
        if e.slot is not Slot.BN or not e.player_id:
            continue
        p = index.by_id(e.player_id)
        if p is None or p.injury_status in HARD or p.injury_status is InjuryStatus.DOUBTFUL:
            continue
        if not p.team or p.team in schedule.teams_on_bye or schedule.game_for(p.team) is None:
            continue
        if schedule.is_locked(p.team, now):
            continue
        out.append(p)
    return out


def audit_team(team: Team, starters: list[RosterEntry], index: PlayerIndex, schedule: WeekSchedule, now: datetime,
               values: dict | None = None) -> list[Issue]:
    """Issues in one team's starting lineup for this week, with a bench fix when one exists."""
    bench = _available_bench(team, index, schedule, now)
    used: set[str] = set()
    issues: list[Issue] = []

    def best_fix(slot: Slot) -> str | None:
        cands = [p for p in bench if p.eligible_for(slot) and p.id not in used]
        if not cands:
            return None
        if values:
            cands.sort(key=lambda p: -(values.get(p.id).ppg if values.get(p.id) else 0.0))
        used.add(cands[0].id)
        return cands[0].name

    for e in starters:
        if e.player_id is None:
            fix = best_fix(e.slot)
            issues.append(Issue(e.slot, "empty", None, "(empty)", "empty slot", fixable=fix is not None, fix=fix, locked=False))
            continue
        p = index.by_id(e.player_id)
        if p is None:
            continue
        locked = bool(p.team and schedule.is_locked(p.team, now))
        kind = detail = None
        if not p.team or p.team in schedule.teams_on_bye or schedule.game_for(p.team) is None:
            kind, detail = "bye", "on bye" if p.team in schedule.teams_on_bye else "no game this week"
        elif p.injury_status in HARD:
            kind, detail = "out", f"{str(p.injury_status).upper() if p.injury_status is InjuryStatus.IR else str(p.injury_status).capitalize()}" + (f" ({p.injury_note})" if p.injury_note else "")
        elif p.injury_status is InjuryStatus.DOUBTFUL:
            kind, detail = "doubtful", "Doubtful" + (f" ({p.injury_note})" if p.injury_note else "")
        if kind is None:
            continue
        fix = None if locked else best_fix(e.slot)
        issues.append(Issue(e.slot, kind, p.id, p.name, detail, fixable=(not locked) and fix is not None, fix=fix, locked=locked))
    return issues


def notice_message(owner: str, team_name: str, issues: list[Issue]) -> str:
    """A short, friendly heads-up the manager can paste to the other manager."""
    fixable = [i for i in issues if not i.locked]
    if not fixable:
        return ""
    parts = []
    for i in fixable:
        what = f"{i.player_name} ({i.slot}) is {i.detail}" if i.kind != "empty" else f"your {i.slot} slot is empty"
        parts.append(what + (f" — {i.fix} is on your bench" if i.fix else ""))
    who = owner or team_name
    return f"Hey {who}, heads up before kickoff: " + "; ".join(parts) + "."
