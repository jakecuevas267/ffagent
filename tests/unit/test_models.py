import pytest

from ffagent.domain.models import (
    InjuryStatus,
    Platform,
    Player,
    Position,
    RosterEntry,
    Slot,
    Team,
    slot_accepts,
)


class TestInjuryStatus:
    @pytest.mark.parametrize("raw,expected", [
        (None, InjuryStatus.HEALTHY),
        ("", InjuryStatus.HEALTHY),
        ("Questionable", InjuryStatus.QUESTIONABLE),
        ("Doubtful", InjuryStatus.DOUBTFUL),
        ("Out", InjuryStatus.OUT),
        ("IR", InjuryStatus.IR),
        ("PUP", InjuryStatus.PUP),
        ("Sus", InjuryStatus.SUSPENDED),
        ("NA", InjuryStatus.NA),
        ("questionable", InjuryStatus.QUESTIONABLE),
    ])
    def test_parse(self, raw, expected):
        assert InjuryStatus.parse(raw) is expected

    def test_unknown_string_is_an_error_not_a_guess(self):
        with pytest.raises(ValueError):
            InjuryStatus.parse("Bruised ego")

    @pytest.mark.parametrize("status", [InjuryStatus.OUT, InjuryStatus.IR, InjuryStatus.PUP,
                                        InjuryStatus.SUSPENDED, InjuryStatus.NA])
    def test_unplayable(self, status):
        assert status.unplayable

    @pytest.mark.parametrize("status", [InjuryStatus.HEALTHY, InjuryStatus.QUESTIONABLE,
                                        InjuryStatus.DOUBTFUL])
    def test_playable(self, status):
        assert not status.unplayable


class TestSlotEligibility:
    @pytest.mark.parametrize("slot,pos,ok", [
        (Slot.QB, Position.QB, True),
        (Slot.QB, Position.RB, False),
        (Slot.FLEX, Position.RB, True),
        (Slot.FLEX, Position.WR, True),
        (Slot.FLEX, Position.TE, True),
        (Slot.FLEX, Position.QB, False),
        (Slot.SUPER_FLEX, Position.QB, True),
        (Slot.SUPER_FLEX, Position.K, False),
        (Slot.REC_FLEX, Position.WR, True),
        (Slot.REC_FLEX, Position.RB, False),
        (Slot.DEF, Position.DEF, True),
        (Slot.BN, Position.K, True),
        (Slot.IR, Position.QB, True),
    ])
    def test_slot_accepts(self, slot, pos, ok):
        assert slot_accepts(slot, pos) is ok

    def test_multi_position_player(self):
        p = Player(id="1", name="Taysom Hill", positions=[Position.TE, Position.QB], team="NO")
        assert p.eligible_for(Slot.TE)
        assert p.eligible_for(Slot.SUPER_FLEX)
        assert not p.eligible_for(Slot.RB)


class TestTeam:
    def test_starters_exclude_bench_ir_and_taxi(self):
        team = Team(id="1", name="A", roster=[
            RosterEntry(player_id="a", slot=Slot.QB),
            RosterEntry(player_id="b", slot=Slot.BN),
            RosterEntry(player_id="c", slot=Slot.IR),
            RosterEntry(player_id="d", slot=Slot.TAXI),
            RosterEntry(player_id="e", slot=Slot.FLEX),
        ])
        assert [e.player_id for e in team.starters] == ["a", "e"]
        assert [e.player_id for e in team.bench] == ["b"]

    def test_empty_starting_slot_is_representable(self):
        e = RosterEntry(player_id=None, slot=Slot.RB)
        assert e.is_empty and e.is_starter


class TestPlayer:
    def test_external_ids(self):
        p = Player(id="6790", name="D'Andre Swift", positions=[Position.RB], team="CHI",
                   external_ids={Platform.ESPN: "4259545"})
        assert p.external_id(Platform.ESPN) == "4259545"
        assert p.external_id(Platform.SLEEPER) == "6790"
