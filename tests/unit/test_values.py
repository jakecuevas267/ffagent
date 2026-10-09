from ffagent.domain.models import LeagueRef, Platform
from ffagent.sources.projections import Projection
from ffagent.sources.values import ValueGateway

REF = LeagueRef(platform=Platform.SLEEPER, league_id="1", season=2026, my_team_id="1")
SCORING = {"rec": 1.0, "rec_yd": 0.1, "rush_yd": 0.1}


class Weekly:
    def week(self, ref, week):
        return {"a": Projection("a", week, {"rec": 5, "rec_yd": 50}), "b": Projection("b", week, {"rush_yd": 80})}


class Season:
    name = "Expert"

    def __init__(self, fail=False):
        self.fail = fail

    def season(self, ref):
        if self.fail:
            raise RuntimeError("boom")
        return {"a": Projection("a", 0, {"rec": 85, "rec_yd": 850})}  # 170 pts / 17 = 10 ppg


def test_default_only():
    gw = ValueGateway(Weekly(), None, "Sleeper")
    v = gw.values(REF, 6, SCORING)
    assert v["a"].ppg == 10.0 and v["b"].ppg == 8.0 and "Sleeper week 6" in v["a"].source
    assert gw.line() == "values: Sleeper next-week projections"


def test_expert_overrides_per_player():
    gw = ValueGateway(Weekly(), Season(), "Sleeper")
    v = gw.values(REF, 6, SCORING)
    assert v["a"].source == "Expert season" and v["a"].ppg == 10.0
    assert v["b"].source == "Sleeper week 6"
    assert gw.line().startswith("values: Expert season projections for 1 players, Sleeper next-week for 1")


def test_expert_failure_falls_back():
    gw = ValueGateway(Weekly(), Season(fail=True), "Sleeper")
    v = gw.values(REF, 6, SCORING)
    assert v["a"].source == "Sleeper week 6" and "failed: RuntimeError" in gw.line()
