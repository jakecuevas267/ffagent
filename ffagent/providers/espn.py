"""ESPN fantasy football provider over the unofficial v3 league endpoint."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ffagent.domain.models import (
    Format,
    InjuryStatus,
    LeagueRef,
    LeagueSettings,
    Matchup,
    Platform,
    Player,
    Position,
    RosterEntry,
    Slot,
    Team,
    Transaction,
    WaiverType,
)
from ffagent.providers.espn_client import ESPNClient
from ffagent.sources.projections import Projection

VIEWS = ["mSettings", "mTeam", "mRoster", "mMatchup", "mStatus"]
APPLIED = "espn_applied"  # ESPN already scores projections under the league's rules

SLOTS = {0: Slot.QB, 2: Slot.RB, 4: Slot.WR, 6: Slot.TE, 7: Slot.SUPER_FLEX, 16: Slot.DEF, 17: Slot.K,
         20: Slot.BN, 21: Slot.IR, 23: Slot.FLEX}
POSITIONS = {1: Position.QB, 2: Position.RB, 3: Position.WR, 4: Position.TE, 5: Position.K, 16: Position.DEF}
PRO_TEAMS = {1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET", 9: "GB", 10: "TEN",
             11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG",
             20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WAS",
             29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU"}
INJURY = {None: InjuryStatus.HEALTHY, "ACTIVE": InjuryStatus.HEALTHY, "NORMAL": InjuryStatus.HEALTHY,
          "DAY_TO_DAY": InjuryStatus.QUESTIONABLE, "QUESTIONABLE": InjuryStatus.QUESTIONABLE,
          "DOUBTFUL": InjuryStatus.DOUBTFUL, "OUT": InjuryStatus.OUT, "INJURY_RESERVE": InjuryStatus.IR,
          "SUSPENSION": InjuryStatus.SUSPENDED, "PROBABLE": InjuryStatus.HEALTHY}
_TX_KINDS = {"WAIVER": "waiver", "FREEAGENT": "free_agent", "TRADE_ACCEPT": "trade", "TRADE": "trade"}
_TX_STATUS = {"EXECUTED": "complete", "PENDING": "pending", "CANCELED": "canceled"}
WAIVERS = {"WAIVERS_CONTINUOUS": WaiverType.ROLLING, "WAIVERS_TRADITIONAL": WaiverType.REVERSE_STANDINGS,
           "FREEAGENCY": WaiverType.NONE}


def _expert_points_key(settings: dict) -> str:
    """Which source points total matches this league: fp_points_ppr / fp_points_half / fp_points."""
    kind = (settings.get("scoringSettings", {}).get("playerRankType") or "STANDARD").upper()
    if "HALF" in kind:
        return "fp_points_half"
    if "PPR" in kind:
        return "fp_points_ppr"
    return "fp_points"


def player_id(espn_id: int | str) -> str:
    return f"espn:{espn_id}"


def map_player(pl: dict) -> Player:
    pos = POSITIONS.get(pl.get("defaultPositionId"))
    return Player(
        id=player_id(pl["id"]), name=pl.get("fullName") or f"{pl.get('firstName', '')} {pl.get('lastName', '')}".strip(),
        positions=[pos] if pos else [], team=PRO_TEAMS.get(pl.get("proTeamId")),
        injury_status=INJURY.get(pl.get("injuryStatus"), InjuryStatus.HEALTHY),
        injury_note="day-to-day" if pl.get("injuryStatus") == "DAY_TO_DAY" else None,
        active=bool(pl.get("active", True)), external_ids={Platform.ESPN: str(pl["id"])},
    )


def weekly_projection(pl: dict, week: int) -> float | None:
    for s in pl.get("stats", []):
        if s.get("scoringPeriodId") == week and s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1:
            return float(s.get("appliedTotal") or 0.0)
    return None


class ESPNProvider:
    def __init__(self, client: ESPNClient | Any):
        self._c = client
        self._cache: dict[str, dict] = {}
        self._pool: dict[tuple[str, int], list[dict]] = {}

    @classmethod
    def from_env(cls, season: int) -> ESPNProvider:
        return cls(ESPNClient.from_env(season))

    def _league(self, ref: LeagueRef) -> dict:
        if ref.league_id not in self._cache:
            self._cache[ref.league_id] = self._c.league(ref.league_id, VIEWS)
        return self._cache[ref.league_id]

    def refresh(self) -> None:
        self._cache.clear()
        self._pool.clear()

    # -- LeagueProvider ------------------------------------------------------------------------
    def league_name(self, ref: LeagueRef) -> str:
        return self._league(ref)["settings"].get("name", "")

    def find_my_team(self, ref: LeagueRef, username: str) -> str | None:
        want = username.lower()
        lg = self._league(ref)
        members = {m["id"]: m for m in lg.get("members", [])}
        for t in lg["teams"]:
            if any(members.get(o, {}).get("displayName", "").lower() == want for o in t.get("owners", [])):
                return str(t["id"])
        raise LookupError(f"{ref.key}: no manager named {username!r}")

    def current_week(self) -> int:
        lg = next(iter(self._cache.values()), None)
        if lg is None:
            raise RuntimeError("current_week needs a league loaded first")
        return int(lg["status"]["currentMatchupPeriod"])

    def settings(self, ref: LeagueRef) -> LeagueSettings:
        st = self._league(ref)["settings"]
        slots: dict[Slot, int] = {}
        for sid, n in st["rosterSettings"]["lineupSlotCounts"].items():
            if n and int(sid) in SLOTS:
                slots[SLOTS[int(sid)]] = int(n)
        aq = st.get("acquisitionSettings", {})
        faab = bool(aq.get("isUsingAcquisitionBudget"))
        keepers = st.get("draftSettings", {}).get("keeperCount", 0) or 0
        deadline = st.get("tradeSettings", {}).get("deadlineDate")
        return LeagueSettings(
            num_teams=int(st["size"]), roster_slots=slots, scoring={APPLIED: 1.0, _expert_points_key(st): 1.0},
            format=Format.KEEPER if keepers else Format.REDRAFT,
            waiver_type=WaiverType.FAAB if faab else WAIVERS.get(aq.get("acquisitionType"), WaiverType.NONE),
            faab_budget=aq.get("acquisitionBudget") if faab else None,
            trade_deadline=datetime.fromtimestamp(deadline / 1000, tz=UTC) if deadline else None,
            playoff_start_week=(st.get("scheduleSettings", {}).get("matchupPeriodCount") or 0) + 1 or None,
        )

    def teams(self, ref: LeagueRef) -> list[Team]:
        lg = self._league(ref)
        settings = self.settings(ref)
        members = {m["id"]: m.get("displayName", "") for m in lg.get("members", [])}
        out = []
        for t in lg["teams"]:
            rec = t.get("record", {}).get("overall", {})
            spent = t.get("transactionCounter", {}).get("acquisitionBudgetSpent", 0)
            out.append(Team(
                id=str(t["id"]), name=t.get("name") or t.get("abbrev", f"Team {t['id']}"),
                owner=", ".join(members.get(o, o) for o in t.get("owners", [])),
                roster=[RosterEntry(player_id=player_id(e["playerId"]), slot=SLOTS.get(e["lineupSlotId"], Slot.BN))
                        for e in t["roster"]["entries"]],
                wins=rec.get("wins", 0), losses=rec.get("losses", 0), ties=rec.get("ties", 0),
                points_for=float(rec.get("pointsFor", 0)),
                faab_remaining=(settings.faab_budget - spent) if settings.faab_budget is not None else None,
                waiver_priority=t.get("waiverRank"),
            ))
        return out

    def lineups(self, ref: LeagueRef, week: int) -> dict[str, list[RosterEntry]]:
        return {t.id: t.starters for t in self.teams(ref)}

    def matchup(self, ref: LeagueRef, week: int) -> Matchup:
        if ref.my_team_id is None:
            raise ValueError(f"{ref.key}: my_team_id is not set")
        me = int(ref.my_team_id)
        for m in self._league(ref).get("schedule", []):
            if m.get("matchupPeriodId") != week:
                continue
            home, away = m.get("home", {}), m.get("away", {})
            if me in (home.get("teamId"), away.get("teamId")):
                mine, opp = (home, away) if home.get("teamId") == me else (away, home)
                return Matchup(week=week, my_team_id=ref.my_team_id,
                               opponent_team_id=str(opp["teamId"]) if opp.get("teamId") is not None else None,
                               my_points=float(mine.get("totalPoints") or 0), opponent_points=float(opp.get("totalPoints") or 0))
        return Matchup(week=week, my_team_id=ref.my_team_id, opponent_team_id=None)

    def players(self) -> list[Player]:
        """Players known from loaded leagues' rosters (ESPN has no cheap global dump)."""
        seen: dict[str, Player] = {}
        for lg in self._cache.values():
            for t in lg["teams"]:
                for e in t["roster"]["entries"]:
                    p = map_player(e["playerPoolEntry"]["player"])
                    seen[p.id] = p
        for pool in self._pool.values():
            for e in pool:
                p = map_player(e["player"])
                seen.setdefault(p.id, p)
        return list(seen.values())

    def _player_pool(self, ref: LeagueRef, week: int) -> list[dict]:
        key = (ref.league_id, week)
        if key not in self._pool:
            self._pool[key] = self._c.players(ref.league_id, week)
        return self._pool[key]

    def free_agents(self, ref: LeagueRef, week: int | None = None) -> list[Player]:
        week = week or self.current_week()
        out = []
        for e in self._player_pool(ref, week):
            if e.get("status") in ("FREEAGENT", "WAIVERS"):
                pl = map_player(e["player"])
                pl.on_waivers = e["status"] == "WAIVERS"
                out.append(pl)
        return out

    def transactions(self, ref: LeagueRef, week: int) -> list[Transaction]:
        """Waiver claims (incl. pending), free-agent adds and trades for a scoring period."""
        out = []
        for t in self._c.transactions(ref.league_id):
            if t.get("scoringPeriodId") != week or t.get("type") not in _TX_KINDS:
                continue
            adds = {player_id(i["playerId"]): str(i["toTeamId"]) for i in t.get("items", []) if i.get("type") == "ADD"}
            drops = {player_id(i["playerId"]): str(i["fromTeamId"]) for i in t.get("items", []) if i.get("type") == "DROP"}
            out.append(Transaction(
                id=str(t["id"]), kind=_TX_KINDS[t["type"]], status=_TX_STATUS.get(t.get("status"), "failed"),
                team_ids=sorted({str(t.get("teamId"))} | set(adds.values()) | set(drops.values()) - {"0"}),
                adds=adds, drops=drops, faab_bid=t.get("bidAmount") if t["type"] == "WAIVER" else None,
                created=datetime.fromtimestamp(t["proposedDate"] / 1000, tz=UTC) if t.get("proposedDate") else None,
            ))
        return out

    def projections(self, ref: LeagueRef, week: int) -> dict[str, Projection]:
        """ESPN's weekly projections, already applied under this league's scoring."""
        out: dict[str, Projection] = {}
        for t in self._league(ref)["teams"]:
            for e in t["roster"]["entries"]:
                pl = e["playerPoolEntry"]["player"]
                pts = weekly_projection(pl, week)
                if pts is not None:
                    out[player_id(pl["id"])] = Projection(player_id=player_id(pl["id"]), week=week,
                                                          stats={APPLIED: pts}, team=PRO_TEAMS.get(pl.get("proTeamId")))
        for e in self._pool.get((ref.league_id, week), []):
            pl = e["player"]
            pts = weekly_projection(pl, week)
            if pts is not None:
                out.setdefault(player_id(pl["id"]), Projection(player_id=player_id(pl["id"]), week=week,
                                                               stats={APPLIED: pts}, team=PRO_TEAMS.get(pl.get("proTeamId"))))
        return out


class ESPNProjections:
    """Adapts a provider's league-scored projections to the `week(ref, week)` source interface."""

    def __init__(self, provider: ESPNProvider):
        self._p = provider

    def week(self, ref: LeagueRef, week: int) -> dict[str, Projection]:
        return self._p.projections(ref, week)
