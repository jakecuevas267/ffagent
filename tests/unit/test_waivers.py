import pytest

from ffagent.analysis.waivers import (
    Claim,
    Valued,
    drop_candidates,
    evaluate_add,
    plan_claims,
    size_bid,
)
from ffagent.domain.models import InjuryStatus, LeagueSettings, Player, Position, Slot, WaiverType

SLOTS = {Slot.QB: 1, Slot.RB: 2, Slot.WR: 2, Slot.TE: 1, Slot.FLEX: 1, Slot.K: 1, Slot.DEF: 1, Slot.BN: 3, Slot.IR: 1}


def settings(waiver=WaiverType.FAAB):
    return LeagueSettings(num_teams=10, roster_slots=SLOTS, waiver_type=waiver, faab_budget=100 if waiver is WaiverType.FAAB else None)


def V(id, pos, value, slot=None, injury=InjuryStatus.HEALTHY, droppable=True, trending=0, bye_next=False):
    p = Player(id=id, name=id, positions=[pos], team="X", injury_status=injury)
    return Valued(player=p, value=value, raw_value=value, source="t", on_roster_slot=slot, droppable=droppable,
                  trending=trending, bye_next=bye_next)


@pytest.fixture
def roster():
    return [
        V("qb1", Position.QB, 18, Slot.QB), V("rb1", Position.RB, 15, Slot.RB), V("rb2", Position.RB, 11, Slot.RB),
        V("wr1", Position.WR, 14, Slot.WR), V("wr2", Position.WR, 12, Slot.WR), V("te1", Position.TE, 9, Slot.TE),
        V("wr3", Position.WR, 10, Slot.FLEX), V("k1", Position.K, 8, Slot.K), V("def1", Position.DEF, 7, Slot.DEF),
        V("rb3", Position.RB, 6, Slot.BN), V("wr4", Position.WR, 5, Slot.BN), V("qb2", Position.QB, 12, Slot.BN),
        V("ir1", Position.WR, 0, Slot.IR, injury=InjuryStatus.IR),
    ]


class TestDrops:
    def test_worst_first_never_ir_never_undroppable(self, roster):
        roster[10] = V("wr4", Position.WR, 5, Slot.BN, droppable=False)
        ids = [v.player.id for v in drop_candidates(settings(), roster)]
        assert ids[0] == "rb3" and "ir1" not in ids and "wr4" not in ids

    def test_only_kicker_is_not_droppable_because_slot_would_be_empty(self, roster):
        ids = [v.player.id for v in drop_candidates(settings(), roster)]
        assert "k1" not in ids and "def1" not in ids
        assert "qb1" in ids  # qb2 can fill QB, so the starter is technically cuttable

    def test_backup_qb_is_droppable(self, roster):
        assert "qb2" in [v.player.id for v in drop_candidates(settings(), roster)]


class TestEvaluate:
    def test_free_agent_who_would_start_has_full_gain(self, roster):
        fa = V("fa_rb", Position.RB, 13)   # beats rb2 (11) -> starts at RB, rb2 to FLEX over wr3 (10)
        c = evaluate_add(settings(), roster, fa, drop_candidates(settings(), roster))
        assert c.role == "starter" and c.drop.player.id == "wr4"
        assert c.gain == pytest.approx(13 - 10)  # lineup gain: fa in, wr3 out of FLEX

    def test_depth_pickup_is_valued_over_replacement(self, roster):
        fa = V("fa_rb", Position.RB, 8)    # better than rb3 (6) but would not start
        repl = {Position.RB: 5.0, Position.WR: 6.0}  # the wire offers 5-ppg RBs and 6-ppg WRs freely
        c = evaluate_add(settings(), roster, fa, drop_candidates(settings(), roster), repl)
        # fa vorp = 8 - 5 = 3; drop wr4 (5) vorp = 5 - 6 < 0 -> 0; gain = 0.4 * 3
        assert c.role == "depth" and c.gain == pytest.approx(0.4 * 3)

    def test_backup_qb_in_a_one_qb_league_is_worth_little(self, roster):
        fa = V("fa_qb", Position.QB, 17.5)  # big raw number, but the wire is full of 17-ppg QBs
        repl = {Position.QB: 17.0, Position.WR: 4.0}
        c = evaluate_add(settings(), roster, fa, drop_candidates(settings(), roster), repl)
        # fa over replacement = 0.5; the drop (wr4, 5 vs 4 replacement) was worth 1: nothing gained
        assert c.role == "depth" and c.gain == 0

    def test_bye_cover_bonus(self, roster):
        roster[1] = V("rb1", Position.RB, 15, Slot.RB, bye_next=True)
        fa = V("fa_rb", Position.RB, 8)
        repl = {Position.RB: 5.0, Position.WR: 6.0}
        c = evaluate_add(settings(), roster, fa, drop_candidates(settings(), roster), repl)
        assert c.role == "bye cover" and c.gain == pytest.approx(0.4 * 3 + 2.0)

    def test_replacement_levels(self):
        from ffagent.analysis.waivers import replacement_levels
        fas = [V("a", Position.QB, 20), V("b", Position.QB, 18), V("c", Position.QB, 16), V("d", Position.QB, 10), V("e", Position.RB, 7)]
        r = replacement_levels(fas, depth=3)
        assert r[Position.QB] == 16 and r[Position.RB] == 0.0

    def test_worse_than_every_drop_has_no_gain(self, roster):
        fa = V("fa_wr", Position.WR, 3)
        c = evaluate_add(settings(), roster, fa, drop_candidates(settings(), roster))
        assert c.gain == 0


class TestBids:
    def test_tiers_and_caps(self):
        big = Claim(add=None, drop=None, gain=6, displaces=None, role="starter")
        assert size_bid(big, 100, [40, 55]) == 15
        assert size_bid(big, 100, [10, 5]) == 11          # one more than the richest rival can pay
        assert size_bid(big, 8, [50]) == 1                 # 15% of 8 rounds to 1
        assert size_bid(big, 60, [100]) == 9               # never more than I have / share of remaining
        small = Claim(add=None, drop=None, gain=1.2, displaces=None, role="depth")
        assert size_bid(small, 100, [50]) == 3
        assert size_bid(small, 10, [50], minimum=1) == 1


class TestPlan:
    def test_orders_by_gain_and_limits_count(self, roster):
        fas = [V("a", Position.RB, 13, trending=5000), V("b", Position.WR, 16), V("c", Position.TE, 9.5), V("d", Position.WR, 2)]
        claims = plan_claims(settings(), roster, fas, my_budget=100, rival_budgets=[80, 60], max_claims=2)
        assert [c.add.player.id for c in claims] == ["b", "a"]
        assert all(c.bid is not None for c in claims)
        assert "5,000 adds" in claims[1].notes[0]

    def test_priority_league_marks_only_clear_upgrades(self, roster):
        fas = [V("a", Position.WR, 16), V("b", Position.RB, 8)]
        claims = plan_claims(settings(WaiverType.ROLLING), roster, fas, my_budget=None, rival_budgets=[], max_claims=3)
        by = {c.add.player.id: c for c in claims}
        assert by["a"].use_priority and by["a"].bid is None
        assert not by["b"].use_priority

    def test_known_free_agent_never_uses_priority(self, roster):
        fa = V("a", Position.WR, 16)
        fa.on_waivers = False
        claims = plan_claims(settings(WaiverType.ROLLING), roster, [fa], my_budget=None, rival_budgets=[])
        assert claims and not claims[0].use_priority

    def test_pending_claims_are_skipped(self, roster):
        fas = [V("a", Position.WR, 16)]
        assert plan_claims(settings(), roster, fas, 100, [], pending_adds={"a"}) == []

    def test_nothing_below_min_gain(self, roster):
        fas = [V("a", Position.WR, 5.5)]
        assert plan_claims(settings(), roster, fas, 100, []) == []
