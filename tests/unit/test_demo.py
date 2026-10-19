from ffagent.cli import main


def test_demo_runs_every_workflow_without_credentials(capsys, monkeypatch):
    for var in ("ESPN_S2", "ESPN_SWID", "FANTASY_INFORMATION_SOURCE_API_KEY", "FANTASY_INFORMATION_SOURCE_KEY",
                "FANTASY_INFORMATION_SOURCE_URL", "ANTHROPIC_API_KEY", "FFAGENT_SECRETS", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FFAGENT_NO_LLM", "1")
    assert main(["demo", "--no-llm"]) == 0
    out = capsys.readouterr().out
    for section in ("LINEUP", "INJURY", "WAIVERS", "TRADES", "AUDIT"):
        assert section in out
    assert "Demo Sleeper League" in out and "Demo ESPN League" in out
    assert "To do in the app" in out            # at least one approved checklist
    assert "Jonathan Taylor is out" in out       # the injury demo swap
    assert "projections: FantasyAPISource for" in out  # expert gateway on fixtures
    assert "demo state written under" in out


def test_demo_single_workflow(capsys, monkeypatch):
    monkeypatch.setenv("FFAGENT_NO_LLM", "1")
    assert main(["demo", "--workflow", "audit", "--no-llm"]) == 0
    out = capsys.readouterr().out
    assert "AUDIT" in out and "LINEUP" not in out
