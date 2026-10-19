"""Demo mode: every workflow on recorded fixtures, with a fixed clock. No credentials, no network.

Fixtures are the same files the test suite uses: a synthetic Sleeper league built on a real player
database and two scrubbed ESPN leagues, plus projections, rankings, trends, stats and a schedule."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ffagent.config import Config
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import InjuryStatus
from ffagent.graphs.lineup import LineupDeps
from ffagent.providers.espn import ESPNProjections, ESPNProvider
from ffagent.providers.sleeper import SleeperProvider
from ffagent.schedule.nfl import parse_scoreboard
from ffagent.sources.fantasy_api_source import FantasyAPISource
from ffagent.sources.gateway import ProjectionGateway
from ffagent.sources.projections import parse_projections
from ffagent.sources.stats import SleeperStats
from ffagent.sources.trending import SleeperTrends
from ffagent.sources.values import ValueGateway

CLOCKS = {  # one realistic moment per workflow, inside the recorded week 5 (2026)
    "lineup": "2026-10-07T18:00:00-06:00",   # Wednesday evening, before Thursday's game
    "injury": "2026-10-11T09:00:00-06:00",   # Sunday morning
    "waivers": "2026-10-13T09:00:00-06:00",  # Tuesday morning
    "trades": "2026-10-14T09:00:00-06:00",   # Wednesday morning
    "audit": "2026-10-10T12:00:00-06:00",    # Saturday noon
}


def fixtures_dir() -> Path:
    env = os.environ.get("FFAGENT_FIXTURES")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    if here.exists():
        return here
    raise FileNotFoundError("demo fixtures not found; set FFAGENT_FIXTURES to the tests/fixtures directory")


def _load(path: Path):
    return json.loads(path.read_text())


class FixtureSleeperClient:
    def __init__(self, root: Path):
        self.r = root / "sleeper"

    def league(self, league_id):           return _load(self.r / "league.json")
    def rosters(self, league_id):          return _load(self.r / "rosters.json")
    def users(self, league_id):            return _load(self.r / "users.json")
    def matchups(self, league_id, week):   return _load(self.r / "matchups_w5.json")
    def transactions(self, league_id, w):  return _load(self.r / "transactions_w5.json")
    def players(self):                     return _load(self.r / "players.json")
    def state(self):                       return _load(self.r / "state.json")
    def trending(self, kind="add", lookback_hours=24, limit=25): return _load(self.r / f"trending_{kind}.json")


class FixtureESPNClient:
    def __init__(self, root: Path):
        self.r, self.season = root / "espn", 2026

    def league(self, league_id, views, scoring_period=None): return _load(self.r / f"league_{league_id}.json")
    def players(self, league_id, scoring_period, limit=2000): return _load(self.r / "players_100001_w5.json")
    def transactions(self, league_id):     return _load(self.r / "transactions_100001.json")["transactions"]


class FixtureExpertClient:
    def __init__(self, root: Path):
        self.r = root / "fantasy_api_source"
        self.calls = 0

    def projections(self, season, week, position, scoring="PPR"):
        name = "projections_season.json" if week == 0 else "projections_w5.json"
        return _load(self.r / name).get(position, [])

    def rankings(self, season, position, scoring="PPR", week=None):
        if week is None:
            return _load(self.r / "rankings_ros.json").get(position, [])
        return []  # no weekly rankings recorded: streaming falls back to projections


class FixtureStats(SleeperStats):
    def __init__(self, root: Path):
        super().__init__(http=None)
        self.data = _load(root / "sleeper" / "stats_w3_w5.json")

    def week(self, season, week):
        return self.data.get(str(week), {})


class FixtureProjections:
    def __init__(self, root: Path):
        self.data = parse_projections(_load(root / "sleeper" / "projections_w5.json"))

    def week(self, ref, week):
        return self.data


def demo_config(data_dir: Path) -> Config:
    return Config.model_validate({
        "season": 2026, "timezone": "America/Denver", "data_dir": str(data_dir),
        "leagues": [{"platform": "sleeper", "league_id": "1000000000000000001", "me": "alice", "name": "Demo Sleeper League"},
                    {"platform": "espn", "league_id": "100001", "team_id": "5", "name": "Demo ESPN League"}],
    })


def build_demo_providers(root: Path | None = None) -> dict[str, object]:
    root = root or fixtures_dir()
    sleeper = SleeperProvider(FixtureSleeperClient(root))
    # Make the demo interesting: one of my Sleeper starters is Out this week.
    for p in sleeper.players():
        if p.name == "Jonathan Taylor":
            p.injury_status, p.injury_note = InjuryStatus.OUT, "ankle"
    return {"sleeper": sleeper, "espn": ESPNProvider(FixtureESPNClient(root))}


def build_demo_deps(config: Config, providers: dict[str, object], store=None, root: Path | None = None) -> dict[str, LineupDeps]:
    root = root or fixtures_dir()
    sched = parse_scoreboard(_load(root / "espn" / "scoreboard_2026_w5.json"))
    nfl = providers["sleeper"]
    trends, stats = SleeperTrends(nfl), FixtureStats(root)
    expert_client = FixtureExpertClient(root)
    deps = {}
    for plat, p in providers.items():
        default = ESPNProjections(p) if plat == "espn" else FixtureProjections(root)
        name = "ESPN" if plat == "espn" else "Sleeper"
        expert = FantasyAPISource(expert_client, lambda ref, p=p: PlayerIndex(p.players()))
        deps[plat] = LineupDeps(p, ProjectionGateway(default, expert, name), lambda s, w: sched, store=store,
                                values=ValueGateway(default, expert, name), trends=trends, nfl=nfl, stats=stats,
                                adviser=_adviser(config))
    return deps


def _adviser(config: Config):
    from ffagent.cli import build_adviser
    return build_adviser(config, "waivers")
