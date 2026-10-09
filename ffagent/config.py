"""ffagent.yaml: leagues, sources, notifiers, schedule. Secrets never live here; see .env."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from ffagent.domain.models import LeagueRef, Platform


class LeagueConfig(BaseModel):
    platform: Platform
    league_id: str
    me: str | None = None         # Sleeper username; resolved to a roster id at runtime
    team_id: str | None = None    # explicit team/roster id (required for ESPN)
    name: str = ""

    @field_validator("league_id", "team_id", mode="before")
    @classmethod
    def _str(cls, v):
        return None if v is None else str(v)

    @model_validator(mode="after")
    def _identity(self):
        if self.me is None and self.team_id is None:
            raise ValueError(f"{self.platform}:{self.league_id}: set `me` (Sleeper username) or `team_id`")
        return self

    def to_ref(self, season: int) -> LeagueRef:
        return LeagueRef(platform=self.platform, league_id=self.league_id, season=season,
                         my_team_id=self.team_id, name=self.name)


class LLMConfig(BaseModel):
    model: str = "anthropic:claude-fable-5-1"
    temperature: float = 0.0


class SourceConfig(BaseModel):
    type: str
    options: dict = Field(default_factory=dict)


class NotifyConfig(BaseModel):
    type: str = "stdout"
    options: dict = Field(default_factory=dict)


class ScheduleConfig(BaseModel):
    waivers_lead_hours: int = 18
    lineup_lead_hours: int = 24
    inactives_lead_minutes: int = 75
    morning_check_hour: int = 9


class WaiverConfig(BaseModel):
    max_claims: int = 3


class Config(BaseModel):
    season: int
    timezone: str = "America/New_York"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    leagues: list[LeagueConfig] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)
    notify: list[NotifyConfig] = Field(default_factory=list)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    waivers: WaiverConfig = Field(default_factory=WaiverConfig)
    data_dir: Path = Path("data")
    expert_cache_hours: float = 6.0  # expert projections are re-fetched at most this often


def load_config(path: Path | str) -> Config:
    path = Path(path)
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
