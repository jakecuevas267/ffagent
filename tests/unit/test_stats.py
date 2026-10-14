import json
from pathlib import Path

from ffagent.sources.stats import SleeperStats

FIX = Path(__file__).parent.parent / "fixtures" / "sleeper"
DATA = json.loads((FIX / "stats_w3_w5.json").read_text())
SCORING = json.loads((FIX / "league.json").read_text())["scoring_settings"]


class FakeStats(SleeperStats):
    def __init__(self):
        super().__init__(http=None)
        self.calls = 0

    def week(self, season, week):
        self.calls += 1
        return DATA.get(str(week), {})


def test_recent_points_snaps_targets_under_league_scoring():
    r = FakeStats().recent(2026, 5, SCORING)
    assert "4984" in r  # Josh Allen
    allen = r["4984"]
    assert 1 <= allen.weeks <= 3 and len(allen.points) == allen.weeks
    assert allen.avg_points > 10 and allen.snap_share is not None and 0.5 < allen.snap_share <= 1.0
    assert "avg" in allen.line() and "snaps" in allen.line()


def test_six_point_passing_td_changes_actuals():
    base = FakeStats().recent(2026, 5, SCORING)["4984"].avg_points
    six = FakeStats().recent(2026, 5, SCORING | {"pass_td": 6}).recent if False else FakeStats().recent(2026, 5, SCORING | {"pass_td": 6})["4984"].avg_points
    assert six > base


def test_window_respects_week_one():
    s = FakeStats()
    s.recent(2026, 2, SCORING, window=3)
    assert s.calls == 2  # weeks 1 and 2 only
