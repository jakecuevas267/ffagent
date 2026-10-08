import textwrap

import pytest

from ffagent.config import Config, load_config
from ffagent.domain.models import Platform

EXAMPLE = textwrap.dedent("""
    season: 2026
    timezone: America/Denver
    llm:
      model: "anthropic:claude-fable-5-1"
    leagues:
      - platform: sleeper
        league_id: "1000000000000000001"
        me: alice
      - platform: espn
        league_id: 123456
        team_id: 4
        name: Work League
    schedule:
      waivers_lead_hours: 12
""")


def test_load_example(tmp_path):
    f = tmp_path / "ffagent.yaml"
    f.write_text(EXAMPLE)
    cfg = load_config(f)
    assert cfg.season == 2026 and cfg.timezone == "America/Denver"
    assert cfg.llm.model == "anthropic:claude-fable-5-1"
    assert len(cfg.leagues) == 2
    s, e = cfg.leagues
    assert s.platform is Platform.SLEEPER and s.league_id == "1000000000000000001" and s.me == "alice"
    assert e.platform is Platform.ESPN and e.league_id == "123456" and e.team_id == "4"
    assert e.name == "Work League"


def test_schedule_defaults_and_overrides(tmp_path):
    f = tmp_path / "ffagent.yaml"
    f.write_text(EXAMPLE)
    cfg = load_config(f)
    assert cfg.schedule.waivers_lead_hours == 12
    assert cfg.schedule.lineup_lead_hours == 24
    assert cfg.schedule.inactives_lead_minutes == 75


def test_league_ref_conversion(tmp_path):
    f = tmp_path / "ffagent.yaml"
    f.write_text(EXAMPLE)
    cfg = load_config(f)
    ref = cfg.leagues[1].to_ref(cfg.season)
    assert ref.platform is Platform.ESPN and ref.my_team_id == "4" and ref.season == 2026
    assert cfg.leagues[0].to_ref(cfg.season).my_team_id is None  # resolved later from username


def test_unknown_platform_is_an_error():
    with pytest.raises(ValueError):
        Config.model_validate({"season": 2026, "leagues": [{"platform": "yahoo", "league_id": "1"}]})


def test_sleeper_league_needs_me_or_team_id():
    with pytest.raises(ValueError):
        Config.model_validate({"season": 2026, "leagues": [{"platform": "sleeper", "league_id": "1"}]})


def test_minimal_config_has_no_sources_or_notifiers():
    cfg = Config.model_validate({"season": 2026, "leagues": []})
    assert cfg.sources == [] and cfg.notify == []
