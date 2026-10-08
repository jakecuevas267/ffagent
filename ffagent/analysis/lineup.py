"""Deterministic lineup optimization.

Choosing the best set of starters is a transversal-matroid problem (a player's points do not depend
on the slot), so greedy-by-points with a matching feasibility check is optimal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ffagent.domain.models import InjuryStatus, Player, Slot


@dataclass
class Candidate:
    player: Player
    points: float
    current_slot: Slot | None = None  # None = not on this roster view
    available: bool = True            # False: bye, out, IR, no game
    locked: bool = False              # game already started: cannot move in or out
    reason: str | None = None         # why unavailable / flagged

    @property
    def is_current_starter(self) -> bool:
        return self.current_slot is not None and self.current_slot not in (Slot.BN, Slot.IR, Slot.TAXI)


@dataclass
class Assignment:
    slot: Slot
    player_id: str | None
    points: float


@dataclass
class LineupResult:
    assignments: list[Assignment]
    moves: list[tuple[str, str | None, Slot]]                      # (player in, player out, slot)
    projected_before: float
    projected_after: float
    warnings: list[str] = field(default_factory=list)
    flags: list[tuple[str, str, str]] = field(default_factory=list)  # (player_id, status, reason)
    close_calls: list[tuple[str, str, Slot, float]] = field(default_factory=list)  # (chosen, runner-up, slot, margin)

    @property
    def optimal_already(self) -> bool:
        return not self.moves


def _match(chosen: list[Candidate], slots: list[Slot], fixed: dict[int, Candidate]) -> dict[int, Candidate] | None:
    """Assign each chosen candidate to a distinct eligible slot (Kuhn's algorithm). None if impossible."""
    assign: dict[int, Candidate] = dict(fixed)
    free = [i for i in range(len(slots)) if i not in fixed]

    def try_place(c: Candidate, seen: set[int]) -> bool:
        for i in free:
            if i in seen or not c.player.eligible_for(slots[i]):
                continue
            seen.add(i)
            if i not in assign or try_place(assign[i], seen):
                assign[i] = c
                return True
        return False

    for c in chosen:
        if not try_place(c, set()):
            return None
    return assign


def optimize_lineup(slots: list[Slot], candidates: list[Candidate], close_margin: float = 1.0) -> LineupResult:
    current: dict[int, Candidate] = {}
    used: set[str] = set()
    for i, slot in enumerate(slots):
        for c in candidates:
            if c.current_slot is slot and c.player.id not in used:
                current[i] = c
                used.add(c.player.id)
                break

    fixed: dict[int, Candidate] = {}
    for i, c in current.items():
        if c.locked:
            if not c.player.eligible_for(slots[i]):
                raise ValueError(f"{c.player.id} is locked in slot {slots[i]} but not eligible for it")
            fixed[i] = c

    pool = sorted((c for c in candidates if c.available and not c.locked and c.player.id not in
                   {f.player.id for f in fixed.values()}), key=lambda c: (-c.points, c.player.id))
    chosen: list[Candidate] = []
    for c in pool:
        if _match(chosen + [c], slots, fixed) is not None:
            chosen.append(c)

    # The chosen set is optimal; now place it so that current starters keep their slots wherever
    # that is still feasible, so the diff only shows real moves.
    chosen_ids = {c.player.id for c in chosen}
    pins = dict(fixed)
    for i, c in current.items():
        if i in pins or c.player.id not in chosen_ids:
            continue
        trial = pins | {i: c}
        rest = [x for x in chosen if x.player.id not in {t.player.id for t in trial.values()}]
        if _match(rest, slots, trial) is not None:
            pins = trial
    rest = [x for x in chosen if x.player.id not in {t.player.id for t in pins.values()}]
    best = _match(rest, slots, pins) or dict(pins)

    warnings: list[str] = []
    assignments: list[Assignment] = []
    for i, slot in enumerate(slots):
        c = best.get(i)
        if c is None and i in current:
            c = current[i]  # nobody better to put in: leave as is, but say so
            warnings.append(f"{c.player.id} stays in {slot}: {c.reason or 'unavailable'} and no replacement on the bench")
            best[i] = c
        assignments.append(Assignment(slot, c.player.id if c else None, c.points if c and c.available else 0.0))

    moves = []
    for i, slot in enumerate(slots):
        new, old = best.get(i), current.get(i)
        if new is not None and (old is None or new.player.id != old.player.id):
            moves.append((new.player.id, old.player.id if old else None, slot))

    before = sum(c.points for c in current.values() if c.available)
    after = sum(a.points for a in assignments)

    flags = [(c.player.id, str(c.player.injury_status), c.reason or c.player.injury_note or "see injury report")
             for c in best.values() if c.player.injury_status in (InjuryStatus.QUESTIONABLE, InjuryStatus.DOUBTFUL)]

    starters = {c.player.id for c in best.values()}
    close = []
    for i, slot in enumerate(slots):
        c = best.get(i)
        if c is None or i in fixed:
            continue
        runner = max((x for x in pool if x.player.id not in starters and x.player.eligible_for(slot)),
                     key=lambda x: x.points, default=None)
        if runner is not None and 0 <= c.points - runner.points <= close_margin:
            close.append((c.player.id, runner.player.id, slot, c.points - runner.points))

    return LineupResult(assignments, moves, before, after, warnings, flags, close)
