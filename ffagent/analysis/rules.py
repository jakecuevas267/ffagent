"""Named roster rules from the manager's playbook. Deterministic, explainable, tested from real cases.

Each rule returns a short name when it applies; proposals cite those names. The LLM advise node
works inside these constraints, never around them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ffagent.domain.models import Format, InjuryStatus, LeagueSettings, Player, Position, Slot

TOP_RANK = {Position.QB: 12, Position.TE: 12, Position.RB: 24, Position.WR: 24}
SEASON_ENDING = {InjuryStatus.NA}          # statuses that mean "gone"; IR/PUP are holds with a slot
SHORT_TERM = {InjuryStatus.QUESTIONABLE, InjuryStatus.DOUBTFUL, InjuryStatus.OUT}


@dataclass
class RosterContext:
    settings: LeagueSettings
    my_starters: set[str]                       # player ids in my projection-optimal lineup
    injured_starter_positions: set[Position]    # positions where one of my starters is hurt
    counts: dict[Position, int] = field(default_factory=dict)   # how many of each position I roster


def hold_reasons(p: Player, ctx: RosterContext) -> list[str]:
    """Why this player must not be dropped (empty list = droppable)."""
    reasons = []
    top = TOP_RANK.get(p.position)
    if top and p.ros_pos_rank is not None and p.ros_pos_rank <= top:
        reasons.append(f"positional-value hold ({p.position}{p.ros_pos_rank} rest-of-season, top {top})")
    if p.injury_status in SHORT_TERM and (p.id in ctx.my_starters or p.is_nfl_starter or
                                          (p.ros_pos_rank is not None and top and p.ros_pos_rank <= top * 1.5)):
        reasons.append(f"injured-starter hold ({p.injury_status}; park on IR when allowed, drop only if out for the season)")
    if p.injury_status in (InjuryStatus.IR, InjuryStatus.PUP) and ctx.settings.format is Format.DYNASTY:
        reasons.append("dynasty hold (injured starter-caliber players keep next-year value)")
    if p.is_nfl_starter and p.position in (Position.RB, Position.WR, Position.TE, Position.QB):
        reasons.append(f"NFL-starter hold ({p.team} {p.depth_position}#1)")
    if p.id in ctx.my_starters:
        reasons.append("fantasy-starter hold (in my lineup; only a starter upgrade may replace him)")
    return reasons


def potential_reasons(p: Player, ctx: RosterContext) -> list[str]:
    """Why a bench player is worth keeping over a flier (soft: a clear upgrade may still replace him)."""
    out = []
    if p.is_rookie_or_second_year and p.position in (Position.RB, Position.WR, Position.TE):
        out.append("rookie/second-year ramp" + (" (dynasty: valued higher)" if ctx.settings.format is Format.DYNASTY else ""))
    if p.position is Position.RB and p.depth_order == 2:
        out.append(f"handcuff ({p.team} RB#2, one injury from starting)")
    return out


def construction_drops(players: list[Player], ctx: RosterContext) -> dict[str, str]:
    """Players whose position is over-stocked: natural drop candidates, with the rule name."""
    out: dict[str, str] = {}
    by_pos: dict[Position, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.position, []).append(p)
    superflex = Slot.SUPER_FLEX in ctx.settings.roster_slots
    limits = {Position.K: 1, Position.DEF: 1, Position.QB: 3 if superflex else 2, Position.TE: 2}
    for pos, limit in limits.items():
        group = sorted(by_pos.get(pos, []), key=lambda p: (p.ros_pos_rank or 999))
        for extra in group[limit:]:
            out[extra.id] = f"roster-construction (more than {limit} {pos})"
    return out


def swap_allowed(add: Player, drop: Player) -> str | None:
    """None if the add/drop pair is fine; otherwise the rule it breaks."""
    if drop.position in (Position.K, Position.DEF) and add.position != drop.position:
        return f"one-{drop.position} rule: a {drop.position} is only replaced by another {drop.position}"
    if add.position in (Position.K, Position.DEF) and drop.position not in (Position.K, Position.DEF):
        return f"never drop a skill player for a {add.position}"
    return None


def pickup_flags(fa: Player, teammates: list[Player]) -> list[str]:
    """Why this free agent deserves attention regardless of projection."""
    flags = []
    if fa.is_nfl_starter and fa.position is Position.RB:
        flags.append(f"team RB1 ({fa.team})")
    if fa.is_nfl_starter and fa.position is Position.WR:
        flags.append(f"starting WR ({fa.team} {fa.depth_position}#1)")
    if fa.position is Position.RB and fa.depth_order == 2:
        starter = next((t for t in teammates if t.position is Position.RB and t.depth_order == 1), None)
        if starter is not None and starter.injury_status.unplayable or (starter is not None and starter.injury_status is InjuryStatus.DOUBTFUL):
            flags.append(f"handcuff opportunity ({starter.name} is {starter.injury_status})")
        elif starter is not None:
            flags.append(f"handcuff ({starter.name}'s backup)")
    if fa.is_rookie_or_second_year:
        flags.append("rookie/second-year upside")
    return flags


def same_backfield(a: Player, b: Player) -> bool:
    return a.position is Position.RB and b.position is Position.RB and a.team is not None and a.team == b.team


def need_multiplier(pos: Position, ctx: RosterContext) -> float:
    """Depth at a position where one of my starters is hurt is worth more."""
    return 1.5 if pos in ctx.injured_starter_positions else 1.0
