"""Game-day injury handling: necessary moves only, status snapshots, and verification of approved moves."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ffagent.analysis.lineup import Candidate, LineupResult, optimize_lineup
from ffagent.domain.models import InjuryStatus, Slot

_BENCH_WORTHY = {InjuryStatus.OUT, InjuryStatus.IR, InjuryStatus.PUP, InjuryStatus.SUSPENDED,
                 InjuryStatus.NA, InjuryStatus.DOUBTFUL}


def injury_moves(slots: list[Slot], candidates: list[Candidate]) -> LineupResult:
    """Fill only the slots whose starter cannot play. Healthy starters stay where they are."""
    pinned = []
    for c in candidates:
        needs_out = (not c.available) or c.player.injury_status in _BENCH_WORTHY
        if c.is_current_starter and not needs_out:
            pinned.append(Candidate(c.player, c.points, c.current_slot, c.available, locked=True, reason=c.reason))
        elif c.is_current_starter and needs_out:
            pinned.append(Candidate(c.player, c.points, c.current_slot, available=False, locked=c.locked,
                                    reason=c.reason or str(c.player.injury_status)))
        else:
            pinned.append(c)
    result = optimize_lineup(slots, pinned, close_margin=0.0)
    result.close_calls = []
    # Name the problem slots that got no fix.
    fixed_out = {out for _, out, _ in result.moves}
    for c in pinned:
        if c.is_current_starter and not c.available and c.player.id not in fixed_out:
            msg = f"{c.player.id} is {c.reason or 'unavailable'} at {c.current_slot}: " + (
                "already locked" if c.locked else "no replacement on the bench whose game has not started")
            if not any(c.player.id in w for w in result.warnings):
                result.warnings.append(msg)
            else:
                result.warnings = [msg if c.player.id in w else w for w in result.warnings]
    return result


@dataclass
class Snapshot:
    taken_at: datetime
    statuses: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # player_id -> (status, note)


def snapshot_of(candidates: list[Candidate], taken_at: datetime) -> Snapshot:
    return Snapshot(taken_at, {c.player.id: (str(c.player.injury_status), c.player.injury_note) for c in candidates})


_SEVERITY = {s: i for i, s in enumerate(["healthy", "questionable", "doubtful", "out", "suspended", "na", "pup", "ir"])}


def diff_snapshots(prev: Snapshot | None, curr: Snapshot) -> list[tuple[str, str | None, str, str]]:
    """(player_id, old_status, new_status, 'downgrade'|'upgrade'|'new') for every change."""
    if prev is None:
        return []
    out = []
    for pid, (status, _) in curr.statuses.items():
        old = prev.statuses.get(pid)
        if old is None:
            if status != "healthy":
                out.append((pid, None, status, "new"))
            continue
        if old[0] != status:
            kind = "downgrade" if _SEVERITY.get(status, 0) > _SEVERITY.get(old[0], 0) else "upgrade"
            out.append((pid, old[0], status, kind))
    return out


def verify_lineup(approved: list[dict], starters: dict[Slot, list[str]]) -> list[dict]:
    """Approved lineup moves whose player_in is not in the slot now, i.e. still to be made."""
    pending = []
    for m in approved:
        slot = Slot(m["slot"])
        if m["player_in"] not in starters.get(slot, []):
            pending.append(m)
    return pending


@dataclass
class IRMove:
    direction: str      # to_ir | from_ir
    player_id: str
    reason: str


def ir_moves(team, index, settings) -> list[IRMove]:
    """IR housekeeping: activate healthy players sitting in IR, then park IR-eligible players in open slots."""
    capacity = settings.roster_slots.get(Slot.IR, 0)
    if capacity == 0:
        return []
    moves: list[IRMove] = []
    occupants = [e.player_id for e in team.roster if e.slot is Slot.IR and e.player_id]
    stale = []
    for pid in occupants:
        p = index.by_id(pid)
        if p is not None and not settings.ir_eligible(p.injury_status):
            stale.append(pid)
            moves.append(IRMove("from_ir", pid, f"{p.injury_status}: no longer IR-eligible, league may block other moves until activated"))
    free = capacity - (len(occupants) - len(stale))
    if free <= 0:
        return moves
    candidates = []
    for e in team.roster:
        if e.slot in (Slot.IR, Slot.TAXI) or not e.player_id:
            continue
        p = index.by_id(e.player_id)
        if p is not None and settings.ir_eligible(p.injury_status):
            candidates.append((0 if e.is_starter else 1, e.player_id, p))
    for _, pid, p in sorted(candidates, key=lambda x: (x[0], x[1]))[:free]:
        moves.append(IRMove("to_ir", pid, f"{p.injury_status}" + (f" ({p.injury_note})" if p.injury_note else "")
                            + f"; {free} IR slot{'s' if free != 1 else ''} open"))
    return moves
