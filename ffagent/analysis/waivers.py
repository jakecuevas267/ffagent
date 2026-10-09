"""Waiver analysis: who to add, who to drop, how much to bid. Deterministic.

Values are rest-of-season points per game on one scale for rostered players and free agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ffagent.analysis.lineup import Candidate, optimize_lineup
from ffagent.domain.models import InjuryStatus, LeagueSettings, Player, Slot, WaiverType

INJURY_DISCOUNT = {InjuryStatus.HEALTHY: 1.0, InjuryStatus.QUESTIONABLE: 0.9, InjuryStatus.DOUBTFUL: 0.7,
                   InjuryStatus.OUT: 0.4, InjuryStatus.SUSPENDED: 0.4, InjuryStatus.IR: 0.2, InjuryStatus.PUP: 0.2,
                   InjuryStatus.NA: 0.1}
BENCH_WEIGHT = 0.4        # a pickup who only adds depth is worth this fraction of his value over replacement
REPLACEMENT_DEPTH = 3     # replacement level at a position = the Nth-best free agent there
BYE_COVER_BONUS = 2.0     # points-per-game equivalent for covering a starter's bye next week
MIN_GAIN = 1.0            # below this (ppg) nothing is proposed
CLEAR_GAIN = 4.0          # at or above: worth a waiver priority / a real FAAB bid


@dataclass
class Valued:
    player: Player
    value: float                 # ROS points per game, injury-discounted
    raw_value: float             # before injury discount
    source: str
    on_roster_slot: Slot | None = None   # None for free agents
    droppable: bool = True
    trending: int = 0            # adds in the last 24h across the platform, if known
    bye_next: bool = False
    on_waivers: bool | None = None  # True: must be claimed; False: free agent now; None: unknown (Sleeper)


@dataclass
class Claim:
    add: Valued
    drop: Valued | None
    gain: float
    displaces: str | None        # player id of the starter he would replace, if any
    role: str                    # "starter" | "depth" | "bye cover"
    bid: int | None = None       # FAAB
    use_priority: bool = False   # rolling / reverse-standings leagues: worth burning priority
    notes: list[str] = field(default_factory=list)


def _as_candidates(roster: list[Valued]) -> list[Candidate]:
    return [Candidate(player=v.player, points=v.value, current_slot=v.on_roster_slot, available=True)
            for v in roster if v.on_roster_slot not in (Slot.IR, Slot.TAXI)]


def _lineup_value(settings: LeagueSettings, roster: list[Valued]) -> tuple[float, dict[int, str | None]]:
    """Best possible starting lineup value from these players, and who fills each slot."""
    r = optimize_lineup(settings.starting_slots, _as_candidates(roster), close_margin=0.0)
    return r.projected_after, {i: a.player_id for i, a in enumerate(r.assignments)}


def _can_fill_slots(settings: LeagueSettings, roster: list[Valued]) -> bool:
    r = optimize_lineup(settings.starting_slots, _as_candidates(roster), close_margin=0.0)
    return all(a.player_id is not None for a in r.assignments)


def drop_candidates(settings: LeagueSettings, roster: list[Valued]) -> list[Valued]:
    """Roster players we could cut, worst first, never leaving a starting slot unfillable."""
    out = []
    for v in sorted(roster, key=lambda v: v.value):
        if v.on_roster_slot in (Slot.IR, Slot.TAXI) or not v.droppable:
            continue
        rest = [x for x in roster if x.player.id != v.player.id]
        if _can_fill_slots(settings, rest):
            out.append(v)
    return out


def replacement_levels(free_agents: list[Valued], depth: int = REPLACEMENT_DEPTH) -> dict:
    """Per position, the value freely available on the wire: the depth-th best free agent."""
    by_pos: dict = {}
    for v in sorted(free_agents, key=lambda v: -v.value):
        by_pos.setdefault(v.player.position, []).append(v.value)
    return {pos: (vals[depth - 1] if len(vals) >= depth else 0.0) for pos, vals in by_pos.items()}


def vorp(v: Valued, replacement: dict) -> float:
    return v.value - replacement.get(v.player.position, 0.0)


def evaluate_add(settings: LeagueSettings, roster: list[Valued], fa: Valued, drops: list[Valued],
                 replacement: dict | None = None) -> Claim | None:
    replacement = replacement or {}
    base_value, base_lineup = _lineup_value(settings, roster)
    best: Claim | None = None
    for drop in drops:
        new_roster = [x for x in roster if x.player.id != drop.player.id] + [fa]
        if not _can_fill_slots(settings, new_roster):
            continue
        new_value, new_lineup = _lineup_value(settings, new_roster)
        lineup_gain = new_value - base_value
        starts = fa.player.id in new_lineup.values()
        if starts:
            displaced = next((pid for i, pid in base_lineup.items() if pid and pid != new_lineup.get(i) and pid != drop.player.id), None)
            gain, role = lineup_gain, "starter"
        else:
            # Depth is only worth what the wire cannot already give me, net of what the drop was worth.
            gain = BENCH_WEIGHT * max(0.0, vorp(fa, replacement) - max(0.0, vorp(drop, replacement)))
            role, displaced = "depth", None
        if fa.bye_next is False and any(x.bye_next and x.on_roster_slot and x.on_roster_slot not in (Slot.BN, Slot.IR, Slot.TAXI)
                                        and x.player.position in fa.player.positions for x in roster):
            gain += BYE_COVER_BONUS
            role = role if starts else "bye cover"
        if best is None or gain > best.gain or (gain == best.gain and drop.value < best.drop.value):
            best = Claim(add=fa, drop=drop, gain=round(gain, 2), displaces=displaced, role=role)
        break  # drops are worst-first; the first legal one is the cheapest, and value gain only shrinks with better drops
    return best


def size_bid(claim: Claim, my_budget: int, rival_budgets: list[int], minimum: int = 1) -> int:
    """FAAB: a share of what's left, by how much the pickup matters, capped just above the richest rival."""
    share = 0.15 if claim.gain >= CLEAR_GAIN else 0.07 if claim.gain >= 2 * MIN_GAIN else 0.03
    bid = max(minimum, round(my_budget * share))
    if rival_budgets:
        bid = min(bid, max(rival_budgets) + 1)
    return min(bid, my_budget)


def plan_claims(settings: LeagueSettings, roster: list[Valued], free_agents: list[Valued], my_budget: int | None,
                rival_budgets: list[int], max_claims: int = 3, min_gain: float = MIN_GAIN,
                pending_adds: set[str] = frozenset()) -> list[Claim]:
    drops = drop_candidates(settings, roster)
    replacement = replacement_levels(free_agents)
    claims: list[Claim] = []
    for fa in sorted(free_agents, key=lambda v: (-v.value, -v.trending)):
        if fa.player.id in pending_adds:
            continue
        c = evaluate_add(settings, roster, fa, drops, replacement)
        if c is None or c.gain < min_gain:
            continue
        if settings.waiver_type is WaiverType.FAAB and my_budget is not None:
            c.bid = size_bid(c, my_budget, rival_budgets, minimum=1)
        else:
            # Burn priority only for a clear upgrade, and never for a player who is a free agent already.
            c.use_priority = c.gain >= CLEAR_GAIN and fa.on_waivers is not False
        if fa.trending:
            c.notes.append(f"{fa.trending:,} adds in the last 24h")
        claims.append(c)
        if len(claims) >= max_claims:
            break
    claims.sort(key=lambda c: -c.gain)
    return claims
