"""Trade targets: positional strength per team, complementary partners, offers that help both sides."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from statistics import median

from ffagent.analysis.lineup import Candidate, optimize_lineup
from ffagent.analysis.waivers import Valued
from ffagent.domain.models import LeagueSettings, Position, Slot

SKILL = (Position.QB, Position.RB, Position.WR, Position.TE)
FAIRNESS = 0.30        # |give − get| value may differ by at most this fraction of the larger side
THEIR_DIP = 0.5        # ppg their lineup may lose and still count as "helps both" (a slight overpay)
MIN_MY_GAIN = 1.0      # ppg my lineup must gain
TOP_GIVE = 6           # my most valuable non-core pieces considered as gives
TOP_GET = 6            # their most valuable pieces at my need positions considered as gets


@dataclass
class TeamProfile:
    team_id: str
    name: str
    roster: list[Valued]
    lineup_value: float
    strength: dict[Position, float]        # starters' value per position
    depth: dict[Position, float]           # best bench value per position
    starter_ids: set[str]

    def need(self, pos: Position, med: dict[Position, float]) -> float:
        return max(0.0, med.get(pos, 0.0) - self.strength.get(pos, 0.0))

    def surplus(self, pos: Position, med: dict[Position, float], replacement: dict[Position, float]) -> float:
        over = max(0.0, self.strength.get(pos, 0.0) - med.get(pos, 0.0))
        bench = max(0.0, self.depth.get(pos, 0.0) - replacement.get(pos, 0.0))
        return over + bench


@dataclass
class Offer:
    partner: TeamProfile
    give: list[Valued]
    get: list[Valued]
    my_gain: float
    their_gain: float
    my_need: Position
    their_need: Position
    notes: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return "give:" + "+".join(sorted(v.player.id for v in self.give)) + "|get:" + "+".join(sorted(v.player.id for v in self.get))

    @property
    def value_give(self) -> float:
        return sum(v.value for v in self.give)

    @property
    def value_get(self) -> float:
        return sum(v.value for v in self.get)


def _cands(roster: list[Valued]) -> list[Candidate]:
    return [Candidate(player=v.player, points=v.value, current_slot=v.on_roster_slot, available=True)
            for v in roster if v.on_roster_slot not in (Slot.IR, Slot.TAXI)]


def profile(team_id: str, name: str, roster: list[Valued], settings: LeagueSettings) -> TeamProfile:
    r = optimize_lineup(settings.starting_slots, _cands(roster), close_margin=0.0)
    by_id = {v.player.id: v for v in roster}
    strength: dict[Position, float] = {}
    starters = set()
    for a in r.assignments:
        if a.player_id and a.player_id in by_id:
            pos = by_id[a.player_id].player.position
            strength[pos] = strength.get(pos, 0.0) + a.points
            starters.add(a.player_id)
    depth: dict[Position, float] = {}
    for v in roster:
        if v.player.id not in starters and v.on_roster_slot not in (Slot.IR, Slot.TAXI):
            depth[v.player.position] = max(depth.get(v.player.position, 0.0), v.value)
    return TeamProfile(team_id, name, roster, r.projected_after, strength, depth, starters)


def league_medians(profiles: list[TeamProfile]) -> dict[Position, float]:
    out = {}
    for pos in SKILL:
        vals = [p.strength.get(pos, 0.0) for p in profiles]
        out[pos] = median(vals) if vals else 0.0
    return out


def lineup_value(roster: list[Valued], settings: LeagueSettings) -> float:
    return optimize_lineup(settings.starting_slots, _cands(roster), close_margin=0.0).projected_after


def _swap(roster: list[Valued], out: list[Valued], inn: list[Valued]) -> list[Valued]:
    gone = {v.player.id for v in out}
    kept = [v for v in roster if v.player.id not in gone]
    return kept + [Valued(player=v.player, value=v.value, raw_value=v.raw_value, source=v.source, on_roster_slot=Slot.BN,
                          holds=v.holds, potential=v.potential) for v in inn]


def fair(give: float, get: float, band: float = FAIRNESS) -> bool:
    top = max(give, get, 0.01)
    return abs(give - get) <= band * top


def find_offers(me: TeamProfile, others: list[TeamProfile], settings: LeagueSettings, med: dict[Position, float],
                replacement: dict[Position, float], max_offers: int = 3, allow_two_for_one: bool = True) -> list[Offer]:
    my_needs = sorted(((me.need(p, med), p) for p in SKILL), reverse=True)
    my_needs = [p for n, p in my_needs if n > 0] or [p for _, p in sorted((me.strength.get(p, 0.0), p) for p in SKILL)][:1]
    offers: list[Offer] = []
    untouchable = {v.player.id for v in me.roster if any("positional-value" in h for h in v.holds)}
    gives_pool = sorted((v for v in me.roster if v.on_roster_slot not in (Slot.IR, Slot.TAXI) and v.player.position in SKILL
                         and v.player.id not in untouchable and v.droppable and v.player.droppable),
                        key=lambda v: -v.value)[:TOP_GIVE + 2]
    for o in others:
        their_needs = [p for p in SKILL if o.need(p, med) > 0]
        for my_need in my_needs:
            if o.surplus(my_need, med, replacement) <= 0:
                continue
            gets = sorted((v for v in o.roster if v.player.position is my_need and v.on_roster_slot not in (Slot.IR, Slot.TAXI)),
                          key=lambda v: -v.value)[:TOP_GET]
            gives = [v for v in gives_pool if v.player.position is not my_need][:TOP_GIVE]
            packages = [[g] for g in gives]
            if allow_two_for_one:
                packages += [list(pair) for pair in combinations(gives, 2)]
            for get in gets:
                for pkg in packages:
                    if not fair(sum(v.value for v in pkg), get.value):
                        continue
                    their_need = next((p for p in their_needs if any(v.player.position is p for v in pkg)), None)
                    if their_need is None and not their_needs:
                        their_need = pkg[0].player.position
                    if their_need is None:
                        continue
                    my_after = lineup_value(_swap(me.roster, pkg, [get]), settings)
                    their_after = lineup_value(_swap(o.roster, [get], pkg), settings)
                    my_gain, their_gain = my_after - me.lineup_value, their_after - o.lineup_value
                    if my_gain < MIN_MY_GAIN or their_gain < -THEIR_DIP:
                        continue
                    off = Offer(o, pkg, [get], round(my_gain, 2), round(their_gain, 2), my_need, their_need)
                    if len(pkg) == 2:
                        off.notes.append("2-for-1: frees a roster spot for me, adds depth for them")
                    offers.append(off)
    # best offer per partner first, then by my gain; no player in two offers
    offers.sort(key=lambda x: (-x.my_gain, -x.their_gain))
    picked, used = [], set()
    for off in offers:
        ids = {v.player.id for v in off.give + off.get}
        if ids & used:
            continue
        picked.append(off)
        used |= ids
        if len(picked) >= max_offers:
            break
    return picked
