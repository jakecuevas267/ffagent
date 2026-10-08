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
                        lambda config, providers: {"sleeper": LineupDeps(provider, FakeProjections(proj), lambda s, w: sched)})
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
