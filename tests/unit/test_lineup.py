import pytest

from ffagent.analysis.lineup import Candidate, optimize_lineup
from ffagent.domain.models import InjuryStatus, Player, Position, Slot

STD = [Slot.QB, Slot.RB, Slot.RB, Slot.WR, Slot.WR, Slot.TE, Slot.FLEX, Slot.K, Slot.DEF]


def P(id, pos, team="BUF", injury=InjuryStatus.HEALTHY):
    return Player(id=id, name=id, positions=[pos], team=team, injury_status=injury)


def C(player, pts, slot=None, available=True, locked=False, reason=None):
    return Candidate(player=player, points=pts, current_slot=slot, available=available,
                     locked=locked, reason=reason)


def ids(result):
    return [a.player_id for a in result.assignments]


@pytest.fixture
def roster():
    return [
        C(P("qb1", Position.QB), 20, Slot.QB),
        C(P("qb2", Position.QB), 18, Slot.BN),
        C(P("rb1", Position.RB), 15, Slot.RB),
        C(P("rb2", Position.RB), 12, Slot.RB),
        C(P("rb3", Position.RB), 9, Slot.BN),
        C(P("wr1", Position.WR), 14, Slot.WR),
        C(P("wr2", Position.WR), 11, Slot.WR),
        C(P("wr3", Position.WR), 10, Slot.FLEX),
        C(P("te1", Position.TE), 12, Slot.TE),
        C(P("te2", Position.TE), 13, Slot.BN),
        C(P("k1", Position.K), 8, Slot.K),
        C(P("def1", Position.DEF), 7, Slot.DEF),
    ]


class TestOptimal:
    def test_te2_takes_flex_over_wr3(self, roster):
        r = optimize_lineup(STD, roster)
        assert ids(r)[6] == "te2"
        assert r.moves == [("te2", "wr3", Slot.FLEX)]
        assert r.projected_after == pytest.approx(20 + 15 + 12 + 14 + 11 + 12 + 13 + 8 + 7)
        assert r.projected_before == pytest.approx(r.projected_after - 3)

    def test_already_optimal(self, roster):
        roster[7] = C(P("wr3", Position.WR), 10, Slot.BN)
        roster[9] = C(P("te2", Position.TE), 13, Slot.FLEX)
        r = optimize_lineup(STD, roster)
        assert r.moves == [] and r.optimal_already

    def test_dedicated_slot_refilled_before_flex_is_touched(self, roster):
        # te1 on bye: te2 takes the TE slot; wr3 keeps FLEX (te2 13 + wr3 10 beats any alternative).
        roster[8] = C(P("te1", Position.TE), 0, Slot.TE, available=False, reason="bye")
        r = optimize_lineup(STD, roster)
        got = ids(r)
        assert got[5] == "te2" and got[6] == "wr3"
        assert r.moves == [("te2", "te1", Slot.TE)]

    def test_two_tes_beat_te_plus_weak_wr(self, roster):
        # te1 12 at TE and te2 13 at FLEX (25) beats te2 at TE and wr3 at FLEX (23).
        r = optimize_lineup(STD, roster)
        assert ids(r)[5] == "te1" and ids(r)[6] == "te2"

    def test_superflex_takes_second_qb(self, roster):
        slots = STD + [Slot.SUPER_FLEX]
        r = optimize_lineup(slots, roster)
        assert ids(r)[-1] == "qb2"


class TestAvailability:
    def test_unavailable_starter_is_replaced(self, roster):
        roster[2] = C(P("rb1", Position.RB), 15, Slot.RB, available=False, reason="bye")
        r = optimize_lineup(STD, roster)
        assert "rb1" not in ids(r) and "rb3" in ids(r)
        assert ("rb3", "rb1", Slot.RB) in r.moves

    def test_no_replacement_keeps_current_and_warns(self, roster):
        roster[10] = C(P("k1", Position.K), 0, Slot.K, available=False, reason="out")
        r = optimize_lineup(STD, roster)
        assert ids(r)[7] == "k1"
        assert any("k1" in w and "out" in w for w in r.warnings)
        assert r.moves == [("te2", "wr3", Slot.FLEX)]

    def test_empty_slot_is_filled(self, roster):
        roster[2] = C(P("rb1", Position.RB), 15, Slot.BN)
        r = optimize_lineup(STD, roster)
        assert ids(r).count("rb1") == 1
        assert ("rb1", None, Slot.RB) in r.moves

    def test_questionable_starter_is_flagged_not_benched(self, roster):
        roster[2] = C(P("rb1", Position.RB, injury=InjuryStatus.QUESTIONABLE), 15, Slot.RB, reason="practice report")
        r = optimize_lineup(STD, roster)
        assert "rb1" in ids(r)
        assert ("rb1", "questionable", "practice report") in r.flags


class TestLocks:
    def test_locked_starter_stays_even_if_worse(self, roster):
        roster[7] = C(P("wr3", Position.WR), 10, Slot.FLEX, locked=True)
        roster[9] = C(P("te2", Position.TE), 11.5, Slot.BN)  # better than wr3 for FLEX, worse than te1 for TE
        r = optimize_lineup(STD, roster)
        assert ids(r)[6] == "wr3" and r.moves == []

    def test_locked_bench_player_cannot_come_in(self, roster):
        roster[9] = C(P("te2", Position.TE), 13, Slot.BN, locked=True)
        r = optimize_lineup(STD, roster)
        assert "te2" not in ids(r) and r.moves == []

    def test_locked_starter_in_wrong_slot_is_an_error(self, roster):
        roster[7] = C(P("wr3", Position.WR), 10, Slot.TE, locked=True)
        with pytest.raises(ValueError):
            optimize_lineup(STD, roster)


class TestCloseCalls:
    def test_flex_within_margin_is_a_close_call(self, roster):
        roster[6] = C(P("wr2", Position.WR), 12.6, Slot.WR)    # keeps WR2 ahead of wr3
        roster[7] = C(P("wr3", Position.WR), 12.5, Slot.FLEX)  # wr3 12.5 keeps FLEX over te1 12; te2 13 takes TE
        r = optimize_lineup(STD, roster, close_margin=0.6)
        assert r.moves == [("te2", "te1", Slot.TE)]
        assert r.close_calls == [("wr3", "te1", Slot.FLEX, pytest.approx(0.5))]

    def test_clear_decision_is_not_a_close_call(self, roster):
        r = optimize_lineup(STD, roster, close_margin=0.5)
        assert r.close_calls == []


def test_player_cannot_fill_two_slots():
    slots = [Slot.RB, Slot.FLEX]
    r = optimize_lineup(slots, [C(P("rb1", Position.RB), 20, Slot.RB), C(P("wr1", Position.WR), 5, Slot.BN)])
    assert ids(r) == ["rb1", "wr1"]
