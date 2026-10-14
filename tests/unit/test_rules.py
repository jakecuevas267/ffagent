"""Rules tested from the manager's real cases (playbook/waivers.md, week 6)."""
from ffagent.analysis.rules import (
    RosterContext,
    construction_drops,
    hold_reasons,
    need_multiplier,
    pickup_flags,
    potential_reasons,
    same_backfield,
    swap_allowed,
)
from ffagent.domain.models import Format, InjuryStatus, LeagueSettings, Player, Position, Slot

SLOTS = {Slot.QB: 1, Slot.RB: 2, Slot.WR: 2, Slot.TE: 1, Slot.FLEX: 2, Slot.K: 1, Slot.BN: 6, Slot.IR: 2}


def P(id, pos, team="X", injury=InjuryStatus.HEALTHY, depth=None, order=None, exp=3, rank=None):
    return Player(id=id, name=id, positions=[pos], team=team, injury_status=injury, depth_position=depth,
                  depth_order=order, years_exp=exp, ros_pos_rank=rank)


def ctx(starters=(), injured=(), fmt=Format.REDRAFT, slots=SLOTS):
    return RosterContext(LeagueSettings(num_teams=12, roster_slots=slots, format=fmt), set(starters), set(injured))


class TestHolds:
    def test_adonai_doubtful_but_a_starter_is_held(self):
        # Case "Flier for a Doubtful bench WR": keep him, park on IR once ruled out.
        adonai = P("adonai", Position.WR, "NYJ", InjuryStatus.DOUBTFUL, "RWR", 2, exp=2, rank=40)
        reasons = hold_reasons(adonai, ctx(starters={"adonai"}))
        assert any("injured-starter hold" in r for r in reasons)

    def test_emanuel_wilson_nfl_starter_is_never_a_dead_spot(self):
        # Case "Three RBs for the same drop": he is SEA RB#1 with the starters hurt.
        wilson = P("wilson", Position.RB, "SEA", depth="RB", order=1, rank=35)
        assert any("NFL-starter hold" in r for r in hold_reasons(wilson, ctx()))

    def test_vele_my_starter_is_not_dropped_for_a_stash(self):
        vele = P("vele", Position.WR, "NO", depth="LWR", order=2, rank=50)
        assert any("fantasy-starter hold" in r for r in hold_reasons(vele, ctx(starters={"vele"})))

    def test_top_24_rb_is_held_on_positional_value(self):
        assert any("positional-value hold" in r for r in hold_reasons(P("rb", Position.RB, rank=20), ctx()))
        assert hold_reasons(P("rb", Position.RB, rank=30, depth="RB", order=2), ctx()) == []

    def test_dynasty_holds_ir_players(self):
        p = P("x", Position.WR, injury=InjuryStatus.IR, rank=60)
        assert any("dynasty hold" in r for r in hold_reasons(p, ctx(fmt=Format.DYNASTY)))
        assert hold_reasons(p, ctx(fmt=Format.REDRAFT)) == []


class TestPotential:
    def test_rookie_coleman_is_potential_in_dynasty(self):
        # Case "Fliers in dynasty": hold Coleman to see if he breaks out.
        coleman = P("coleman", Position.WR, "MIA", exp=0, depth="SWR", order=2)
        r = potential_reasons(coleman, ctx(fmt=Format.DYNASTY))
        assert r and "rookie" in r[0] and "dynasty" in r[0]

    def test_handcuff_rb2_is_potential(self):
        allgeier = P("allgeier", Position.RB, "ARI", depth="RB", order=2, exp=4)
        assert any("handcuff" in r for r in potential_reasons(allgeier, ctx()))


class TestConstruction:
    def test_third_qb_is_a_drop_candidate(self):
        # Case "being dropped" flags: Nix is a third QB on the same roster.
        qbs = [P("daniels", Position.QB, rank=5), P("brissett", Position.QB, rank=20), P("nix", Position.QB, rank=14)]
        drops = construction_drops(qbs, ctx())
        assert list(drops) == ["brissett"] and "roster-construction" in drops["brissett"]

    def test_superflex_allows_three_qbs(self):
        qbs = [P("a", Position.QB, rank=5), P("b", Position.QB, rank=12), P("c", Position.QB, rank=20)]
        assert construction_drops(qbs, ctx(slots=SLOTS | {Slot.SUPER_FLEX: 1})) == {}

    def test_second_kicker_is_a_drop(self):
        assert "k2" in construction_drops([P("k1", Position.K, rank=3), P("k2", Position.K, rank=9)], ctx())


class TestSwaps:
    def test_never_drop_a_skill_player_for_a_kicker(self):
        # Case "Spending a roster spot on a kicker upgrade".
        assert swap_allowed(P("smack", Position.K), P("allgeier", Position.RB)) == "never drop a skill player for a K"

    def test_kicker_for_kicker_is_fine(self):
        assert swap_allowed(P("smack", Position.K), P("loop", Position.K)) is None

    def test_defense_only_replaced_by_defense(self):
        assert swap_allowed(P("wr", Position.WR), P("def", Position.DEF)) is not None


class TestPickups:
    def test_team_rb1_is_flagged(self):
        dobbins = P("dobbins", Position.RB, "DEN", depth="RB", order=1)
        assert "team RB1 (DEN)" in pickup_flags(dobbins, [])

    def test_handcuff_opportunity_when_starter_is_out(self):
        harvey = P("harvey", Position.RB, "DEN", depth="RB", order=2)
        dobbins = P("dobbins", Position.RB, "DEN", InjuryStatus.OUT, "RB", 1)
        assert any("handcuff opportunity" in f for f in pickup_flags(harvey, [dobbins]))

    def test_same_backfield(self):
        assert same_backfield(P("a", Position.RB, "DEN"), P("b", Position.RB, "DEN"))
        assert not same_backfield(P("a", Position.RB, "DEN"), P("b", Position.WR, "DEN"))


def test_need_multiplier_for_injured_positions():
    c = ctx(injured={Position.RB})
    assert need_multiplier(Position.RB, c) == 1.5 and need_multiplier(Position.WR, c) == 1.0
