"""Platform-neutral domain model. Providers map into these; nothing downstream sees raw API shapes."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Platform(StrEnum):
    SLEEPER = "sleeper"
    ESPN = "espn"


class Position(StrEnum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DEF = "DEF"
    # IDP positions exist in some leagues; kept so identity resolution can tell them apart.
    DL = "DL"
    LB = "LB"
    DB = "DB"


FANTASY_POSITIONS = {Position.QB, Position.RB, Position.WR, Position.TE, Position.K, Position.DEF}


class Slot(StrEnum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    FLEX = "FLEX"            # RB/WR/TE
    WRRB_FLEX = "WRRB_FLEX"  # RB/WR
    REC_FLEX = "REC_FLEX"    # WR/TE
    SUPER_FLEX = "SUPER_FLEX"  # QB/RB/WR/TE
    K = "K"
    DEF = "DEF"
    DL = "DL"
    LB = "LB"
    DB = "DB"
    IDP_FLEX = "IDP_FLEX"
    BN = "BN"
    IR = "IR"
    TAXI = "TAXI"


NON_STARTING_SLOTS = {Slot.BN, Slot.IR, Slot.TAXI}

_SLOT_POSITIONS: dict[Slot, set[Position]] = {
    Slot.QB: {Position.QB},
    Slot.RB: {Position.RB},
    Slot.WR: {Position.WR},
    Slot.TE: {Position.TE},
    Slot.FLEX: {Position.RB, Position.WR, Position.TE},
    Slot.WRRB_FLEX: {Position.RB, Position.WR},
    Slot.REC_FLEX: {Position.WR, Position.TE},
    Slot.SUPER_FLEX: {Position.QB, Position.RB, Position.WR, Position.TE},
    Slot.K: {Position.K},
    Slot.DEF: {Position.DEF},
    Slot.DL: {Position.DL},
    Slot.LB: {Position.LB},
    Slot.DB: {Position.DB},
    Slot.IDP_FLEX: {Position.DL, Position.LB, Position.DB},
}


def slot_accepts(slot: Slot, position: Position) -> bool:
    if slot in NON_STARTING_SLOTS:
        return True
    return position in _SLOT_POSITIONS[slot]


_INJURY_ALIASES = {
    "": "healthy", "healthy": "healthy", "active": "healthy",
    "questionable": "questionable", "q": "questionable",
    "doubtful": "doubtful", "d": "doubtful",
    "out": "out", "o": "out",
    "ir": "ir", "injury_reserve": "ir", "injured reserve": "ir",
    "pup": "pup",
    "sus": "suspended", "suspended": "suspended", "suspension": "suspended",
    "na": "na", "n/a": "na", "dnr": "na", "cov": "na",
}


class InjuryStatus(StrEnum):
    HEALTHY = "healthy"
    QUESTIONABLE = "questionable"
    DOUBTFUL = "doubtful"
    OUT = "out"
    IR = "ir"
    PUP = "pup"
    SUSPENDED = "suspended"
    NA = "na"  # not with team / practice squad / inactive list

    @classmethod
    def parse(cls, raw: str | None) -> InjuryStatus:
        key = (raw or "").strip().lower()
        try:
            return cls(_INJURY_ALIASES[key])
        except KeyError:
            raise ValueError(f"unknown injury status {raw!r}") from None

    @property
    def unplayable(self) -> bool:
        return self in {InjuryStatus.OUT, InjuryStatus.IR, InjuryStatus.PUP,
                        InjuryStatus.SUSPENDED, InjuryStatus.NA}


class Format(StrEnum):
    REDRAFT = "redraft"
    KEEPER = "keeper"
    DYNASTY = "dynasty"


class WaiverType(StrEnum):
    FAAB = "faab"
    ROLLING = "rolling"
    REVERSE_STANDINGS = "reverse_standings"
    NONE = "none"


class LeagueRef(BaseModel):
    platform: Platform
    league_id: str
    season: int
    my_team_id: str | None = None
    name: str = ""

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.league_id}"


class LeagueSettings(BaseModel):
    num_teams: int
    roster_slots: dict[Slot, int]
    scoring: dict[str, float] = Field(default_factory=dict)
    format: Format = Format.REDRAFT
    waiver_type: WaiverType = WaiverType.NONE
    faab_budget: int | None = None
    trade_deadline_week: int | None = None
    trade_deadline: datetime | None = None
    playoff_start_week: int | None = None
    ir_statuses: set[InjuryStatus] = Field(default_factory=lambda: {InjuryStatus.IR, InjuryStatus.PUP})

    def ir_eligible(self, status: InjuryStatus) -> bool:
        return status in self.ir_statuses

    @property
    def starting_slots(self) -> list[Slot]:
        """Starting slots in roster order, one entry per slot (so RB, RB for two RB slots)."""
        return [s for s, n in self.roster_slots.items() if s not in NON_STARTING_SLOTS for _ in range(n)]


class Player(BaseModel):
    id: str  # canonical id: the Sleeper player id (team abbreviation for defenses)
    name: str
    positions: list[Position]
    team: str | None = None
    injury_status: InjuryStatus = InjuryStatus.HEALTHY
    injury_note: str | None = None
    practice: str | None = None
    active: bool = True
    external_ids: dict[Platform, str] = Field(default_factory=dict)

    @property
    def position(self) -> Position:
        return self.positions[0]

    def eligible_for(self, slot: Slot) -> bool:
        return any(slot_accepts(slot, p) for p in self.positions)

    def external_id(self, platform: Platform) -> str | None:
        if platform is Platform.SLEEPER:
            return self.id
        return self.external_ids.get(platform)


class RosterEntry(BaseModel):
    player_id: str | None  # None = empty slot
    slot: Slot

    @property
    def is_starter(self) -> bool:
        return self.slot not in NON_STARTING_SLOTS

    @property
    def is_empty(self) -> bool:
        return self.player_id is None


class Team(BaseModel):
    id: str
    name: str
    owner: str = ""
    roster: list[RosterEntry] = Field(default_factory=list)
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    faab_remaining: int | None = None
    waiver_priority: int | None = None

    @property
    def starters(self) -> list[RosterEntry]:
        return [e for e in self.roster if e.is_starter]

    @property
    def bench(self) -> list[RosterEntry]:
        return [e for e in self.roster if e.slot is Slot.BN]

    @property
    def player_ids(self) -> set[str]:
        return {e.player_id for e in self.roster if e.player_id}


class Matchup(BaseModel):
    week: int
    my_team_id: str
    opponent_team_id: str | None
    my_points: float = 0.0
    opponent_points: float = 0.0
    my_projected: float | None = None
    opponent_projected: float | None = None


class Transaction(BaseModel):
    id: str
    kind: str  # waiver | free_agent | trade | commissioner
    status: str
    team_ids: list[str]
    adds: dict[str, str] = Field(default_factory=dict)   # player_id -> team_id
    drops: dict[str, str] = Field(default_factory=dict)
    faab_bid: int | None = None
    created: datetime | None = None


class ProposedAction(BaseModel):
    id: str
    kind: str  # lineup_swap | waiver_claim | add_drop | trade_offer | manager_notice
    league_key: str
    payload: dict = Field(default_factory=dict)
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)
    deadline: datetime | None = None


class Decision(BaseModel):
    action_id: str
    verdict: str  # approved | edited | rejected | expired
    note: str = ""
    edited_payload: dict | None = None
