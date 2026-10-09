"""Sleeper: official read-only REST API, no auth. https://docs.sleeper.com"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from ffagent.domain.models import (
    FANTASY_POSITIONS,
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

BASE = "https://api.sleeper.app/v1"

_WAIVER_TYPES = {0: WaiverType.ROLLING, 1: WaiverType.REVERSE_STANDINGS, 2: WaiverType.FAAB}
_FORMATS = {0: Format.REDRAFT, 1: Format.KEEPER, 2: Format.DYNASTY}
_EMPTY = "0"  # Sleeper's marker for an empty starting slot


class SleeperClient:
    """Thin HTTP layer; returns raw JSON. Rate limit: stay well under 1000 calls/min."""

    def __init__(self, http: httpx.Client | None = None):
        self._http = http or httpx.Client(base_url=BASE, timeout=20)

    def _get(self, path: str, **params: Any) -> Any:
        r = self._http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    def state(self) -> dict:                      return self._get("/state/nfl")
    def user(self, username_or_id: str) -> dict:  return self._get(f"/user/{username_or_id}")
    def user_leagues(self, user_id: str, season: int) -> list[dict]:
        return self._get(f"/user/{user_id}/leagues/nfl/{season}")
    def league(self, league_id: str) -> dict:     return self._get(f"/league/{league_id}")
    def rosters(self, league_id: str) -> list[dict]: return self._get(f"/league/{league_id}/rosters")
    def users(self, league_id: str) -> list[dict]:   return self._get(f"/league/{league_id}/users")
    def matchups(self, league_id: str, week: int) -> list[dict]:
        return self._get(f"/league/{league_id}/matchups/{week}")
    def transactions(self, league_id: str, week: int) -> list[dict]:
        return self._get(f"/league/{league_id}/transactions/{week}")
    def players(self) -> dict:                    return self._get("/players/nfl")  # ~5MB, cache it
    def trending(self, kind: str = "add", lookback_hours: int = 24, limit: int = 25) -> list[dict]:
        return self._get(f"/players/nfl/trending/{kind}", lookback_hours=lookback_hours, limit=limit)


def map_players(raw: dict[str, dict]) -> list[Player]:
    """Map the player dump. Keeps active players at fantasy positions only."""
    out: list[Player] = []
    for pid, p in raw.items():
        if not p.get("active"):
            continue
        positions = [Position(x) for x in (p.get("fantasy_positions") or [p.get("position")])
                     if x in Position.__members__]
        if not positions or not any(x in FANTASY_POSITIONS for x in positions):
            continue
        ext = {}
        if p.get("espn_id"):
            ext[Platform.ESPN] = str(p["espn_id"])
        if p.get("yahoo_id"):
            ext["yahoo"] = str(p["yahoo_id"])
        name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        out.append(Player(
            id=str(pid), name=name, positions=positions, team=p.get("team"),
            injury_status=InjuryStatus.parse(p.get("injury_status")),
            injury_note=p.get("injury_body_part"), practice=p.get("practice_participation"),
            active=True, external_ids=ext,
        ))
    return out


class SleeperProvider:
    def __init__(self, client: SleeperClient | Any):
        self._c = client
        self._players: list[Player] | None = None
        self._league_cache: dict[str, dict] = {}

    # -- raw helpers ---------------------------------------------------------------------------
    def _league(self, ref: LeagueRef) -> dict:
        if ref.league_id not in self._league_cache:
            self._league_cache[ref.league_id] = self._c.league(ref.league_id)
        return self._league_cache[ref.league_id]

    # -- LeagueProvider ------------------------------------------------------------------------
    def players(self) -> list[Player]:
        if self._players is None:
            self._players = map_players(self._c.players())
        return self._players

    def current_week(self) -> int:
        return int(self._c.state()["week"])

    def current_season(self) -> int:
        return int(self._c.state()["season"])

    def league_name(self, ref: LeagueRef) -> str:
        return self._league(ref).get("name", "")

    def find_my_team(self, ref: LeagueRef, username: str) -> str | None:
        """Roster id owned (or co-owned) by a Sleeper username. None when the user is in the
        league without a roster, e.g. a commissioner who only runs it."""
        want = username.lower()
        users = [u for u in self._c.users(ref.league_id)
                 if want in {(u.get("display_name") or "").lower(), (u.get("username") or "").lower()}]
        if not users:
            raise LookupError(f"{ref.key}: no user named {username!r} in this league")
        uid = users[0]["user_id"]
        for r in self._c.rosters(ref.league_id):
            if r.get("owner_id") == uid or uid in (r.get("co_owners") or []):
                return str(r["roster_id"])
        return None

    def settings(self, ref: LeagueRef) -> LeagueSettings:
        lg = self._league(ref)
        st = lg.get("settings", {})
        slots: dict[Slot, int] = {}
        for s in lg["roster_positions"]:
            slot = Slot(s)
            slots[slot] = slots.get(slot, 0) + 1
        # IR and taxi are not in roster_positions; they are counts in settings.
        if st.get("reserve_slots"):
            slots[Slot.IR] = int(st["reserve_slots"])
        if st.get("taxi_slots"):
            slots[Slot.TAXI] = int(st["taxi_slots"])
        ir = {InjuryStatus.IR, InjuryStatus.PUP}
        for flag, status in (("reserve_allow_out", InjuryStatus.OUT), ("reserve_allow_doubtful", InjuryStatus.DOUBTFUL),
                             ("reserve_allow_sus", InjuryStatus.SUSPENDED), ("reserve_allow_na", InjuryStatus.NA),
                             ("reserve_allow_dnr", InjuryStatus.NA), ("reserve_allow_cov", InjuryStatus.NA)):
            if st.get(flag):
                ir.add(status)
        waiver = _WAIVER_TYPES.get(st.get("waiver_type"), WaiverType.NONE)
        return LeagueSettings(
            num_teams=lg.get("total_rosters") or st.get("num_teams"),
            roster_slots=slots,
            scoring={k: float(v) for k, v in lg.get("scoring_settings", {}).items()},
            format=_FORMATS.get(st.get("type", 0), Format.REDRAFT),
            waiver_type=waiver,
            faab_budget=st.get("waiver_budget") if waiver is WaiverType.FAAB else None,
            trade_deadline_week=st.get("trade_deadline") or None,
            playoff_start_week=st.get("playoff_week_start") or None,
            ir_statuses=ir,
        )

    def teams(self, ref: LeagueRef) -> list[Team]:
        settings = self.settings(ref)
        users = {u["user_id"]: u for u in self._c.users(ref.league_id)}
        teams = []
        for r in self._c.rosters(ref.league_id):
            u = users.get(r.get("owner_id") or "", {})
            owner = u.get("display_name", "")
            meta = u.get("metadata") or {}
            s = r.get("settings") or {}
            teams.append(Team(
                id=str(r["roster_id"]),
                name=meta.get("team_name") or (f"Team {owner}" if owner else f"Team {r['roster_id']}"),
                owner=owner,
                roster=_roster_entries(r, settings.starting_slots),
                wins=s.get("wins", 0), losses=s.get("losses", 0), ties=s.get("ties", 0),
                points_for=float(s.get("fpts", 0)) + float(s.get("fpts_decimal", 0)) / 100,
                faab_remaining=(settings.faab_budget - s.get("waiver_budget_used", 0))
                if settings.faab_budget is not None else None,
                waiver_priority=s.get("waiver_position"),
            ))
        return teams

    def lineups(self, ref: LeagueRef, week: int) -> dict[str, list[RosterEntry]]:
        slots = self.settings(ref).starting_slots
        return {str(m["roster_id"]): _starters(m.get("starters") or [], slots)
                for m in self._c.matchups(ref.league_id, week)}

    def matchup(self, ref: LeagueRef, week: int) -> Matchup:
        if ref.my_team_id is None:
            raise ValueError(f"{ref.key}: my_team_id is not set")
        ms = self._c.matchups(ref.league_id, week)
        mine = next(m for m in ms if str(m["roster_id"]) == ref.my_team_id)
        opp = next((m for m in ms if m.get("matchup_id") == mine.get("matchup_id")
                    and m is not mine), None)
        return Matchup(
            week=week, my_team_id=ref.my_team_id,
            opponent_team_id=str(opp["roster_id"]) if opp else None,
            my_points=float(mine.get("points") or 0), opponent_points=float(opp.get("points") or 0) if opp else 0.0,
        )

    def free_agents(self, ref: LeagueRef) -> list[Player]:
        rostered = {pid for r in self._c.rosters(ref.league_id) for pid in (r.get("players") or [])}
        return [p for p in self.players() if p.id not in rostered]

    def trending(self, kind: str = "add", lookback_hours: int = 24, limit: int = 25) -> list[tuple[str, int]]:
        """(player_id, count) most added/dropped across all of Sleeper, most first."""
        return [(str(t["player_id"]), int(t["count"])) for t in self._c.trending(kind, lookback_hours, limit)]

    def transactions(self, ref: LeagueRef, week: int) -> list[Transaction]:
        out = []
        for t in self._c.transactions(ref.league_id, week):
            s = t.get("settings") or {}
            out.append(Transaction(
                id=str(t["transaction_id"]), kind=t["type"], status=t["status"],
                team_ids=[str(x) for x in t.get("roster_ids") or []],
                adds={str(k): str(v) for k, v in (t.get("adds") or {}).items()},
                drops={str(k): str(v) for k, v in (t.get("drops") or {}).items()},
                faab_bid=s.get("waiver_bid"),
                created=datetime.fromtimestamp(t["created"] / 1000, tz=UTC) if t.get("created") else None,
            ))
        return out


def _starters(ids: list[str], slots: list[Slot]) -> list[RosterEntry]:
    return [RosterEntry(player_id=None if pid == _EMPTY else str(pid), slot=slot)
            for pid, slot in zip(ids, slots)]


def _roster_entries(r: dict, starting_slots: list[Slot]) -> list[RosterEntry]:
    starters = r.get("starters") or []
    entries = _starters(starters, starting_slots)
    reserve = set(r.get("reserve") or [])
    taxi = set(r.get("taxi") or [])
    started = {pid for pid in starters if pid != _EMPTY}
    for pid in r.get("players") or []:
        if pid in started:
            continue
        slot = Slot.IR if pid in reserve else Slot.TAXI if pid in taxi else Slot.BN
        entries.append(RosterEntry(player_id=str(pid), slot=slot))
    return entries
