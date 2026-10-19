from ffagent.analysis.trades import fair, find_offers, league_medians, profile
from ffagent.analysis.waivers import Valued
from ffagent.domain.models import LeagueSettings, Player, Position, Slot

SLOTS = {Slot.QB: 1, Slot.RB: 2, Slot.WR: 2, Slot.TE: 1, Slot.FLEX: 1, Slot.K: 1, Slot.DEF: 1, Slot.BN: 4}
SETTINGS = LeagueSettings(num_teams=4, roster_slots=SLOTS)


def V(id, pos, value, slot=Slot.BN, holds=()):
    p = Player(id=id, name=id, positions=[pos], team="X")
    return Valued(player=p, value=value, raw_value=value, source="t", on_roster_slot=slot, holds=list(holds))


def team(prefix, qb, rbs, wrs, te=9, k=8, d=7):
    r = [V(f"{prefix}qb", Position.QB, qb, Slot.QB), V(f"{prefix}te", Position.TE, te, Slot.TE),
         V(f"{prefix}k", Position.K, k, Slot.K), V(f"{prefix}def", Position.DEF, d, Slot.DEF)]
    r += [V(f"{prefix}rb{i}", Position.RB, v, Slot.RB if i < 2 else Slot.BN) for i, v in enumerate(rbs)]
    r += [V(f"{prefix}wr{i}", Position.WR, v, Slot.WR if i < 2 else Slot.BN) for i, v in enumerate(wrs)]
    return r


def test_profile_strength_and_depth():
    p = profile("1", "Me", team("m", 20, [15, 12, 9], [14, 11, 10]), SETTINGS)
    assert p.strength[Position.QB] == 20 and p.strength[Position.RB] == 27
    assert p.strength[Position.WR] == 14 + 11 + 10  # wr2 (10) takes FLEX over rb2 (9)
    assert p.depth[Position.RB] == 9 and Position.WR not in p.depth or p.depth.get(Position.WR, 0) == 0


def test_fairness_band():
    assert fair(10, 12) and not fair(10, 20)


def test_complementary_partner_gets_a_one_for_one_offer():
    # Me: RB-poor, WR-rich. Them: WR-poor, RB-rich. League median set by two neutral teams.
    me = profile("1", "Me", team("m", 20, [9, 8, 6], [16, 15, 14, 13]), SETTINGS)
    them = profile("2", "Them", team("t", 20, [16, 15, 14, 13], [9, 8, 6]), SETTINGS)
    n1 = profile("3", "N1", team("a", 20, [12, 11, 8], [12, 11, 9]), SETTINGS)
    n2 = profile("4", "N2", team("b", 20, [12, 11, 8], [12, 11, 9]), SETTINGS)
    med = league_medians([me, them, n1, n2])
    offers = find_offers(me, [them, n1, n2], SETTINGS, med, {Position.RB: 6.0, Position.WR: 6.0})
    assert offers and offers[0].partner.name == "Them"
    o = offers[0]
    assert o.my_need is Position.RB and all(v.player.position is Position.WR for v in o.give)
    assert all(v.player.position is Position.RB for v in o.get)
    assert o.my_gain >= 1.0 and o.their_gain >= -0.5


def test_held_players_are_never_given():
    me_roster = team("m", 20, [9, 8, 6], [16, 15, 14, 13])
    for v in me_roster:
        if v.player.id == "mwr0":
            v.holds = ["positional-value hold (WR3)"]
    me = profile("1", "Me", me_roster, SETTINGS)
    them = profile("2", "Them", team("t", 20, [16, 15, 14, 13], [9, 8, 6]), SETTINGS)
    med = league_medians([me, them])
    offers = find_offers(me, [them], SETTINGS, med, {})
    assert all(v.player.id != "mwr0" for o in offers for v in o.give)


def test_no_offer_when_nothing_helps_me():
    me = profile("1", "Me", team("m", 20, [16, 15, 14], [16, 15, 14]), SETTINGS)
    them = profile("2", "Them", team("t", 18, [9, 8, 6], [9, 8, 6]), SETTINGS)
    assert find_offers(me, [them], SETTINGS, league_medians([me, them]), {}) == []


def test_two_for_one_frees_a_spot():
    me = profile("1", "Me", team("m", 20, [8, 7, 6], [13, 12, 11, 10, 9]), SETTINGS)
    them = profile("2", "Them", team("t", 20, [18, 9, 8], [12, 7, 6]), SETTINGS)
    n = profile("3", "N", team("a", 20, [12, 11], [12, 11]), SETTINGS)
    med = league_medians([me, them, n])
    offers = find_offers(me, [them, n], SETTINGS, med, {Position.RB: 5.0, Position.WR: 5.0})
    assert offers and any(len(o.give) == 2 for o in offers)
    two = next(o for o in offers if len(o.give) == 2)
    assert "2-for-1" in two.notes[0] and two.get[0].player.id == "trb0"


def test_players_appear_in_at_most_one_offer():
    me = profile("1", "Me", team("m", 20, [9, 8, 6], [16, 15, 14, 13]), SETTINGS)
    them = profile("2", "Them", team("t", 20, [16, 15, 14, 13], [9, 8, 6]), SETTINGS)
    other = profile("3", "Other", team("o", 20, [15, 14, 13, 12], [9, 8, 7]), SETTINGS)
    med = league_medians([me, them, other])
    offers = find_offers(me, [them, other], SETTINGS, med, {}, max_offers=3)
    seen = [v.player.id for o in offers for v in o.give + o.get]
    assert len(seen) == len(set(seen))
