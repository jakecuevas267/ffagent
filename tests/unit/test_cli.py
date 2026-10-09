import json
from pathlib import Path

from ffagent.cli import main
from ffagent.providers.sleeper import SleeperProvider
from tests.providers.test_sleeper import FakeClient

FIX = Path(__file__).parent.parent / "fixtures" / "sleeper"


def test_leagues_prints_my_roster(tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "ffagent.yaml"
    cfg.write_text('season: 2026\nleagues:\n  - {platform: sleeper, league_id: "1000000000000000001", me: alice}\n')
    monkeypatch.setattr("ffagent.cli.build_providers",
                        lambda config: {"sleeper": SleeperProvider(FakeClient())})
    assert main(["--config", str(cfg), "leagues"]) == 0
    out = capsys.readouterr().out
    assert "Test League" in out
    assert "Alice's Team" in out and "alice" in out
    assert "Josh Allen" in out and "QB" in out
    assert "3-1" in out
    assert "FAAB 77" in out


def test_leagues_json(tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "ffagent.yaml"
    cfg.write_text('season: 2026\nleagues:\n  - {platform: sleeper, league_id: "1000000000000000001", me: alice}\n')
    monkeypatch.setattr("ffagent.cli.build_providers",
                        lambda config: {"sleeper": SleeperProvider(FakeClient())})
    assert main(["--config", str(cfg), "leagues", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["league"]["my_team_id"] == "1"
    assert data[0]["teams"][0]["starters"][0]["name"] == "Josh Allen"


def test_missing_config_is_a_clear_error(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "nope.yaml"), "leagues"]) == 2
    assert "nope.yaml" in capsys.readouterr().err


def test_commissioner_league_is_shown_without_my_team(tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "ffagent.yaml"
    cfg.write_text('season: 2026\nleagues:\n  - {platform: sleeper, league_id: "1000000000000000001", me: commish}\n')
    monkeypatch.setattr("ffagent.cli.build_providers",
                        lambda config: {"sleeper": SleeperProvider(FakeClient())})
    assert main(["--config", str(cfg), "leagues"]) == 0
    out = capsys.readouterr().out
    assert "commissioner view" in out and "Test League" in out


def _lineup_env(tmp_path, monkeypatch):
    from ffagent.graphs.lineup import LineupDeps
    from ffagent.schedule.nfl import parse_scoreboard
    from ffagent.sources.projections import parse_projections
    from tests.graph.test_lineup_graph import FakeProjections

    cfg = tmp_path / "ffagent.yaml"
    cfg.write_text('season: 2026\ndata_dir: "%s"\nleagues:\n  - {platform: sleeper, league_id: "1000000000000000001", me: alice}\n'
                   % (tmp_path / "data"))
    sched = parse_scoreboard(json.loads((FIX.parent / "espn" / "scoreboard_2026_w5.json").read_text()))
    proj = parse_projections(json.loads((FIX / "projections_w5.json").read_text()))
    provider = SleeperProvider(FakeClient())
    monkeypatch.setattr("ffagent.cli.build_providers", lambda config: {"sleeper": provider})
    monkeypatch.setattr("ffagent.cli.build_lineup_deps",
                        lambda config, providers, store=None: {"sleeper": LineupDeps(provider, FakeProjections(proj), lambda s, w: sched, store=store)})
    return cfg


def test_run_lineup_dry_run_shows_proposals(tmp_path, capsys, monkeypatch):
    cfg = _lineup_env(tmp_path, monkeypatch)
    assert main(["--config", str(cfg), "run", "lineup", "--dry-run", "--now", "2026-10-07T18:00:00-06:00"]) == 0
    out = capsys.readouterr().out
    assert "Test League" in out and "[1] Start " in out and "projected" in out


def test_run_lineup_interactive_then_remembers(tmp_path, capsys, monkeypatch):
    cfg = _lineup_env(tmp_path, monkeypatch)
    answers = iter(["a", "n", "keeping him", "a", "a", "a", "a", "a", "a", "a"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert main(["--config", str(cfg), "run", "lineup", "--now", "2026-10-07T18:00:00-06:00"]) == 0
    out = capsys.readouterr().out
    assert "To do in the app" in out and "[ ] " in out

    assert main(["--config", str(cfg), "run", "lineup", "--now", "2026-10-07T18:00:00-06:00"]) == 0
    out2 = capsys.readouterr().out
    assert "already reviewed" in out2 and "[ ] " in out2


def test_run_lineup_auto_approve(tmp_path, capsys, monkeypatch):
    cfg = _lineup_env(tmp_path, monkeypatch)
    assert main(["--config", str(cfg), "run", "lineup", "--auto-approve", "--now", "2026-10-07T18:00:00-06:00"]) == 0
    assert "To do in the app" in capsys.readouterr().out


def _injury_env(tmp_path, monkeypatch, hurt=None):
    from ffagent.domain.models import InjuryStatus
    from ffagent.graphs.lineup import LineupDeps
    from ffagent.schedule.nfl import parse_scoreboard
    from ffagent.sources.projections import parse_projections
    from tests.graph.test_lineup_graph import FakeProjections

    cfg = tmp_path / "ffagent.yaml"
    cfg.write_text('season: 2026\ndata_dir: "%s"\nleagues:\n  - {platform: sleeper, league_id: "1000000000000000001", me: alice}\n'
                   % (tmp_path / "data"))
    sched = parse_scoreboard(json.loads((FIX.parent / "espn" / "scoreboard_2026_w5.json").read_text()))
    proj = parse_projections(json.loads((FIX / "projections_w5.json").read_text()))
    provider = SleeperProvider(FakeClient())
    for p in provider.players():
        if p.id in (hurt or []):
            p.injury_status = InjuryStatus.OUT
        if p.id == "5872":
            p.injury_status = InjuryStatus.IR  # fixture parks a healthy player in IR; keep IR quiet here
    monkeypatch.setattr("ffagent.cli.build_providers", lambda config: {"sleeper": provider})
    monkeypatch.setattr("ffagent.cli.build_lineup_deps",
                        lambda config, providers, store=None: {"sleeper": LineupDeps(provider, FakeProjections(proj), lambda s, w: sched, store=store)})
    return cfg


def test_run_injury_quiet_when_healthy(tmp_path, capsys, monkeypatch):
    cfg = _injury_env(tmp_path, monkeypatch)
    assert main(["--config", str(cfg), "run", "injury", "--now", "2026-10-11T09:00:00-06:00"]) == 0
    assert "no injury moves needed" in capsys.readouterr().out


def test_run_injury_proposes_and_records(tmp_path, capsys, monkeypatch):
    cfg = _injury_env(tmp_path, monkeypatch, hurt=["6813"])
    monkeypatch.setattr("builtins.input", lambda prompt="": "a")
    assert main(["--config", str(cfg), "run", "injury", "--now", "2026-10-11T09:00:00-06:00"]) == 0
    out = capsys.readouterr().out
    assert "Jonathan Taylor is out" in out and "To do in the app" in out


def test_watch_loops_until_last_kickoff(tmp_path, capsys, monkeypatch):
    cfg = _injury_env(tmp_path, monkeypatch)
    sleeps = []
    monkeypatch.setattr("ffagent.cli._sleep", lambda s: sleeps.append(s))
    # Sunday 12:00 MT: games at 11:00, 14:05, 14:25 and 18:20 MT. Last kickoff 18:20 → ~7 more checks at 60 min.
    assert main(["--config", str(cfg), "run", "injury", "--watch", "--interval", "60",
                 "--now", "2026-10-11T12:00:00-06:00"]) == 0
    out = capsys.readouterr().out
    assert "watch finished" in out
    assert 6 <= len(sleeps) <= 8 and all(s == 3600 for s in sleeps)


def test_localize_renders_timestamps_in_user_timezone():
    from zoneinfo import ZoneInfo

    from ffagent.review.cli import localize
    s = localize("plays at 2026-10-11T17:00:00+00:00 and 2026-10-12T00:20:00Z", ZoneInfo("America/Denver"))
    assert s == "plays at Sun 11:00 MDT and Sun 18:20 MDT"


def test_build_lineup_deps_uses_expert_gateway_only_when_key_is_set(monkeypatch):
    from ffagent.cli import build_lineup_deps
    from ffagent.config import Config
    from ffagent.sources.gateway import ProjectionGateway

    provider = SleeperProvider(FakeClient())
    cfg = Config.model_validate({"season": 2026, "leagues": []})
    monkeypatch.delenv("FANTASY_INFORMATION_SOURCE_API_KEY", raising=False)
    monkeypatch.delenv("FANTASY_INFORMATION_SOURCE_KEY", raising=False)
    deps = build_lineup_deps(cfg, {"sleeper": provider})
    assert isinstance(deps["sleeper"].projections, ProjectionGateway) and deps["sleeper"].projections.expert is None
    monkeypatch.setenv("FANTASY_INFORMATION_SOURCE_API_KEY", "test-key")
    deps = build_lineup_deps(cfg, {"sleeper": provider})
    assert deps["sleeper"].projections.expert is None  # key without a URL is not usable
    monkeypatch.setenv("FANTASY_INFORMATION_SOURCE_URL", "https://example.test/v2/json/nfl")
    deps = build_lineup_deps(cfg, {"sleeper": provider})
    assert deps["sleeper"].projections.expert is not None and deps["sleeper"].projections.expert.name == "FantasyAPISource"
