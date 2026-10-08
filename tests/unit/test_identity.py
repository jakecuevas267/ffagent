import pytest

from ffagent.domain.identity import PlayerIndex, normalize_name
from ffagent.domain.models import Platform, Player, Position


@pytest.mark.parametrize("raw,expected", [
    ("D'Andre Swift", "dandre swift"),
    ("Odell Beckham Jr.", "odell beckham"),
    ("Kenneth Walker III", "kenneth walker"),
    ("Marvin Harrison Jr", "marvin harrison"),
    ("  Ja'Marr   Chase ", "jamarr chase"),
    ("A.J. Brown", "aj brown"),
    ("Amon-Ra St. Brown", "amonra st brown"),
    ("Michael Pittman Jr.", "michael pittman"),
    ("Robert Griffin III", "robert griffin"),
    ("Chase Brown", "chase brown"),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def _p(id, name, pos, team, espn=None):
    ext = {Platform.ESPN: espn} if espn else {}
    return Player(id=id, name=name, positions=[pos], team=team, external_ids=ext)


@pytest.fixture
def index():
    return PlayerIndex([
        _p("1", "Josh Allen", Position.QB, "BUF", espn="3918298"),
        _p("2", "Josh Allen", Position.LB, "JAX"),
        _p("3", "Mike Williams", Position.WR, "LAC"),
        _p("4", "Mike Williams", Position.WR, "PIT"),
        _p("5", "Lamar Jackson", Position.QB, "BAL"),
        _p("6", "Lamar Jackson", Position.DB, "NYG"),
    ])


class TestLookup:
    def test_by_id(self, index):
        assert index.by_id("5").name == "Lamar Jackson"

    def test_by_external_id(self, index):
        assert index.by_external(Platform.ESPN, "3918298").id == "1"
        assert index.by_external(Platform.ESPN, "nope") is None

    def test_missing_id(self, index):
        assert index.by_id("999") is None


class TestResolve:
    def test_exact_name_and_position(self, index):
        r = index.resolve("Josh Allen", Position.QB)
        assert r.status == "exact" and r.player.id == "1"

    def test_position_disambiguates_same_name(self, index):
        r = index.resolve("Lamar Jackson", Position.DB)
        assert r.status == "exact" and r.player.id == "6"

    def test_name_normalization_applies(self, index):
        r = index.resolve("josh  ALLEN", Position.QB)
        assert r.status == "exact" and r.player.id == "1"

    def test_ambiguous_without_team_is_not_guessed(self, index):
        r = index.resolve("Mike Williams", Position.WR)
        assert r.status == "ambiguous" and r.player is None
        assert {c.id for c in r.candidates} == {"3", "4"}

    def test_team_breaks_the_tie(self, index):
        r = index.resolve("Mike Williams", Position.WR, team="PIT")
        assert r.status == "exact" and r.player.id == "4"

    def test_unknown_name_is_unresolved(self, index):
        r = index.resolve("Nobody Here", Position.QB)
        assert r.status == "unresolved" and r.player is None and r.candidates == []

    def test_wrong_team_falls_back_to_unresolved_not_a_guess(self, index):
        # A single name match whose team disagrees is suspicious (traded? stale?); report it,
        # but do not silently pick it.
        r = index.resolve("Josh Allen", Position.QB, team="MIA")
        assert r.status == "ambiguous" and r.player is None
        assert [c.id for c in r.candidates] == ["1"]
