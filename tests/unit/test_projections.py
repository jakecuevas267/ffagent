import json
from pathlib import Path

import pytest

from ffagent.sources.projections import parse_projections, score

FIX = Path(__file__).parent.parent / "fixtures" / "sleeper"
SCORING = json.loads((FIX / "league.json").read_text())["scoring_settings"]


@pytest.fixture
def proj():
    return parse_projections(json.loads((FIX / "projections_w5.json").read_text()))


def test_keyed_by_player_id(proj):
    assert "4984" in proj and proj["4984"].week == 5
    assert proj["4984"].opponent is not None


def test_defenses_and_kickers_present(proj):
    assert "BUF" in proj and "2747" in proj


def test_score_uses_league_scoring():
    stats = {"pass_yd": 250, "pass_td": 2, "pass_int": 1, "rush_yd": 30, "rush_td": 1, "rec": 0, "pts_ppr": 999}
    assert score(stats, SCORING) == pytest.approx(250 * 0.04 + 2 * 4 - 1 + 30 * 0.1 + 6)


def test_six_point_passing_td_changes_the_answer():
    stats = {"pass_td": 2}
    assert score(stats, {"pass_td": 6}) - score(stats, {"pass_td": 4}) == pytest.approx(4)


def test_stats_without_a_scoring_rule_do_not_count():
    assert score({"made_up_stat": 100}, SCORING) == 0


def test_real_projection_scores_in_a_plausible_range(proj):
    pts = score(proj["4984"].stats, SCORING)
    assert 10 < pts < 40
