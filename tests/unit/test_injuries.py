from datetime import UTC, datetime

import pytest

from ffagent.analysis.injuries import (
    Snapshot,
    diff_snapshots,
    injury_moves,
    snapshot_of,
    verify_lineup,
)
from ffagent.analysis.lineup import Candidate
from ffagent.domain.models import InjuryStatus, Player, Position, Slot

STD = [Slot.QB, Slot.RB, Slot.RB, Slot.WR, Slot.WR, Slot.TE, Slot.FLEX, Slot.K, Slot.DEF]


def P(id, pos, injury=InjuryStatus.HEALTHY, team="BUF", note=None):
    return Player(id=id, name=id, positions=[pos], team=team, injury_status=injury, injury_note=note)


def C(player, pts, slot, available=True, locked=False, reason=None):
    return Candidate(player=player, points=pts, current_slot=slot, available=available, locked=locked, reason=reason)


@pytest.fixture
def roster():
    return [
        C(P("qb1", Position.QB), 20, Slot.QB),
        C(P("rb1", Position.RB), 15, Slot.RB),
        C(P("rb2", Position.RB), 12, Slot.RB),
        C(P("wr1", Position.WR), 14, Slot.WR),
        C(P("wr2", Position.WR), 11, Slot.WR),
        C(P("te1", Position.TE), 9, Slot.TE),
        C(P("wr3", Position.WR), 10, Slot.FLEX),
        C(P("k1", Position.K), 8, Slot.K),
        C(P("def1", Position.DEF), 7, Slot.DEF),
        C(P("rb3", Position.RB), 9, Slot.BN),
        C(P("wr4", Position.WR), 13, Slot.BN),   # projects higher than wr2, but wr2 is healthy
        C(P("te2", Position.TE), 6, Slot.BN),
    ]


class TestInjuryMoves:
    def test_healthy_lineup_has_no_moves_even_if_bench_projects_higher(self, roster):
        r = injury_moves(STD, roster)
        assert r.moves == []

    def test_out_starter_replaced_by_best_eligible_bench(self, roster):
        roster[1] = C(P("rb1", Position.RB, InjuryStatus.OUT), 0, Slot.RB, available=False, reason="out")
        r = injury_moves(STD, roster)
        assert r.moves == [("rb3", "rb1", Slot.RB)]

    def test_doubtful_is_treated_as_out(self, roster):
        roster[4] = C(P("wr2", Position.WR, InjuryStatus.DOUBTFUL), 11, Slot.WR, available=False, reason="doubtful")
        r = injury_moves(STD, roster)
        assert r.moves == [("wr4", "wr2", Slot.WR)]

    def test_questionable_is_flagged_not_benched(self, roster):
        roster[4] = C(P("wr2", Position.WR, InjuryStatus.QUESTIONABLE), 11, Slot.WR, reason="hamstring")
        r = injury_moves(STD, roster)
        assert r.moves == [] and ("wr2", "questionable", "hamstring") in r.flags

    def test_empty_slot_is_filled(self, roster):
        del roster[6]  # nobody in FLEX
        r = injury_moves(STD, roster)
        assert r.moves == [("wr4", None, Slot.FLEX)]

    def test_locked_out_starter_cannot_be_replaced(self, roster):
        roster[1] = C(P("rb1", Position.RB, InjuryStatus.OUT), 0, Slot.RB, available=False, locked=True, reason="out")
        r = injury_moves(STD, roster)
        assert r.moves == [] and any("rb1" in w for w in r.warnings)

    def test_bench_player_whose_game_started_cannot_come_in(self, roster):
        roster[1] = C(P("rb1", Position.RB, InjuryStatus.OUT), 0, Slot.RB, available=False, reason="out")
        roster[9] = C(P("rb3", Position.RB), 9, Slot.BN, locked=True)
        r = injury_moves(STD, roster)
        # only wr4 (via FLEX chain) could help, but RB slot needs an RB: no fix available
        assert r.moves == [] and any("rb1" in w for w in r.warnings)

    def test_flex_chain_when_direct_replacement_is_missing(self, roster):
        # te1 out, no healthy TE on the bench except te2 (6); te2 goes in, no chain needed
        roster[5] = C(P("te1", Position.TE, InjuryStatus.OUT), 0, Slot.TE, available=False, reason="out")
        r = injury_moves(STD, roster)
        assert r.moves == [("te2", "te1", Slot.TE)]

    def test_no_eligible_replacement_warns(self, roster):
        roster[0] = C(P("qb1", Position.QB, InjuryStatus.OUT), 0, Slot.QB, available=False, reason="out")
        r = injury_moves(STD, roster)
        assert r.moves == [] and any("qb1" in w and "no replacement" in w for w in r.warnings)


class TestSnapshots:
    def test_snapshot_of_candidates(self, roster):
        roster[4] = C(P("wr2", Position.WR, InjuryStatus.QUESTIONABLE, note="hamstring"), 11, Slot.WR)
        snap = snapshot_of(roster, taken_at=datetime(2026, 10, 11, 14, 0, tzinfo=UTC))
        assert snap.statuses["wr2"] == ("questionable", "hamstring")
        assert snap.statuses["qb1"] == ("healthy", None)

    def test_diff_reports_downgrades_upgrades_and_new(self):
        a = Snapshot(datetime(2026, 10, 11, 9, 0, tzinfo=UTC), {"x": ("healthy", None), "y": ("questionable", "knee"), "z": ("out", None)})
        b = Snapshot(datetime(2026, 10, 11, 15, 0, tzinfo=UTC), {"x": ("questionable", "ankle"), "y": ("healthy", None), "z": ("out", None), "w": ("doubtful", None)})
        d = diff_snapshots(a, b)
        assert ("x", "healthy", "questionable", "downgrade") in d
        assert ("y", "questionable", "healthy", "upgrade") in d
        assert ("w", None, "doubtful", "new") in d
        assert not any(c[0] == "z" for c in d)

    def test_diff_against_nothing_is_empty(self):
        b = Snapshot(datetime(2026, 10, 11, 15, 0, tzinfo=UTC), {"x": ("out", None)})
        assert diff_snapshots(None, b) == []


class TestVerify:
    def test_reports_approved_moves_not_yet_made(self):
        starters = {Slot.RB: ["rb1", "rb2"], Slot.FLEX: ["wr3"]}
        approved = [{"slot": "RB", "player_in": "rb3", "player_out": "rb1"},
                    {"slot": "FLEX", "player_in": "wr3", "player_out": "te2"}]
        pending = verify_lineup(approved, starters)
        assert pending == [approved[0]]

    def test_done_when_player_in_is_in_the_slot(self):
        starters = {Slot.RB: ["rb3", "rb2"]}
        assert verify_lineup([{"slot": "RB", "player_in": "rb3", "player_out": "rb1"}], starters) == []


class TestIRMoves:
    from ffagent.analysis.injuries import ir_moves

    def _settings(self, ir_slots=1, allow=(InjuryStatus.IR, InjuryStatus.OUT)):
        from ffagent.domain.models import LeagueSettings
        return LeagueSettings(num_teams=10, roster_slots={Slot.QB: 1, Slot.BN: 3, Slot.IR: ir_slots},
                              ir_statuses=set(allow))

    def _team(self, entries):
        from ffagent.domain.models import RosterEntry, Team
        return Team(id="1", name="T", roster=[RosterEntry(player_id=pid, slot=slot) for pid, slot in entries])

    def _index(self, *players):
        from ffagent.domain.identity import PlayerIndex
        return PlayerIndex(list(players))

    def test_out_player_can_move_to_open_ir(self):
        from ffagent.analysis.injuries import ir_moves
        team = self._team([("qb1", Slot.QB), ("rb1", Slot.BN)])
        idx = self._index(P("qb1", Position.QB), P("rb1", Position.RB, InjuryStatus.OUT, note="knee"))
        moves = ir_moves(team, idx, self._settings())
        assert [(m.direction, m.player_id) for m in moves] == [("to_ir", "rb1")]
        assert "1 IR slot open" in moves[0].reason

    def test_no_move_when_status_not_allowed_on_ir(self):
        from ffagent.analysis.injuries import ir_moves
        team = self._team([("rb1", Slot.BN)])
        idx = self._index(P("rb1", Position.RB, InjuryStatus.OUT))
        assert ir_moves(team, idx, self._settings(allow=(InjuryStatus.IR,))) == []

    def test_no_move_when_ir_full_and_occupants_still_eligible(self):
        from ffagent.analysis.injuries import ir_moves
        team = self._team([("rb1", Slot.BN), ("wr9", Slot.IR)])
        idx = self._index(P("rb1", Position.RB, InjuryStatus.OUT), P("wr9", Position.WR, InjuryStatus.IR))
        assert ir_moves(team, idx, self._settings(ir_slots=1)) == []

    def test_healthy_player_in_ir_must_be_activated(self):
        from ffagent.analysis.injuries import ir_moves
        team = self._team([("wr9", Slot.IR)])
        idx = self._index(P("wr9", Position.WR))  # healthy now
        moves = ir_moves(team, idx, self._settings())
        assert [(m.direction, m.player_id) for m in moves] == [("from_ir", "wr9")]

    def test_swap_when_ir_full_with_a_healthy_occupant(self):
        from ffagent.analysis.injuries import ir_moves
        team = self._team([("rb1", Slot.BN), ("wr9", Slot.IR)])
        idx = self._index(P("rb1", Position.RB, InjuryStatus.OUT), P("wr9", Position.WR))
        moves = ir_moves(team, idx, self._settings(ir_slots=1))
        assert [(m.direction, m.player_id) for m in moves] == [("from_ir", "wr9"), ("to_ir", "rb1")]

    def test_league_without_ir_slots_proposes_nothing(self):
        from ffagent.analysis.injuries import ir_moves
        team = self._team([("rb1", Slot.BN)])
        idx = self._index(P("rb1", Position.RB, InjuryStatus.OUT))
        assert ir_moves(team, idx, self._settings(ir_slots=0)) == []

    def test_starters_are_suggested_before_bench(self):
        from ffagent.analysis.injuries import ir_moves
        team = self._team([("qb1", Slot.QB), ("rb1", Slot.BN)])
        idx = self._index(P("qb1", Position.QB, InjuryStatus.OUT), P("rb1", Position.RB, InjuryStatus.OUT))
        moves = ir_moves(team, idx, self._settings(ir_slots=1))
        assert [m.player_id for m in moves] == ["qb1"]
