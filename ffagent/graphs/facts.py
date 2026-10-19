"""Facts every judgment workflow needs: expert ranks, values, recent performance, NFL facts on
other platforms' players. Shared by waivers and trades."""
from __future__ import annotations

from ffagent.analysis.rules import RosterContext
from ffagent.analysis.waivers import INJURY_DISCOUNT, Valued, apply_rules
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import Platform


def enrich(deps, ref, index: PlayerIndex, extra_players: list) -> None:
    """Depth chart / experience onto ESPN players via the crosswalk."""
    if ref.platform is Platform.ESPN and deps.nfl is not None:
        from ffagent.providers.sleeper import enrich_from_sleeper
        enrich_from_sleeper(list(index) + extra_players, PlayerIndex(deps.nfl.players()))


def ros_ranks(deps, ref) -> dict[str, int]:
    expert = getattr(deps.values, "expert", None)
    if expert is None or not hasattr(expert, "ros_ranks"):
        return {}
    try:
        return expert.ros_ranks(ref)
    except Exception:  # noqa: BLE001 - a refinement, never a blocker
        return {}


def recent_performance(deps, ref, index: PlayerIndex, extra_players: list, through_week: int, scoring: dict) -> dict:
    if deps.stats is None:
        return {}
    try:
        raw = deps.stats.recent(ref.season, max(1, through_week), scoring)
    except Exception:  # noqa: BLE001
        return {}
    if ref.platform is Platform.SLEEPER or deps.nfl is None:
        return raw
    sidx = PlayerIndex(deps.nfl.players())
    out = {}
    for pl in list(index) + extra_players:
        eid = pl.external_ids.get(Platform.ESPN)
        sp = sidx.by_external(Platform.ESPN, eid) if eid else None
        if sp is not None and sp.id in raw:
            out[pl.id] = raw[sp.id]
    return out


def make_valued(player, values: dict, recent: dict, next_byes: set, slot=None, trending: int = 0) -> Valued:
    v = values.get(player.id)
    raw = v.ppg if v else 0.0
    return Valued(player=player, value=round(raw * INJURY_DISCOUNT.get(player.injury_status, 1.0), 2), raw_value=raw,
                  source=v.source if v else "none", on_roster_slot=slot, trending=trending,
                  bye_next=bool(player.team and player.team in next_byes), on_waivers=player.on_waivers,
                  recent=recent.get(player.id))


def roster_valued(team, index: PlayerIndex, values: dict, recent: dict, next_byes: set) -> list[Valued]:
    return [make_valued(index.by_id(e.player_id), values, recent, next_byes, e.slot)
            for e in team.roster if e.player_id and index.by_id(e.player_id)]


def context_for(settings, team, roster: list[Valued], index: PlayerIndex) -> RosterContext:
    """Hold/potential rules need who starts (by value and in fact) and where the team is hurt."""
    from ffagent.analysis.lineup import Candidate, optimize_lineup
    from ffagent.domain.models import Slot
    cands = [Candidate(player=v.player, points=v.value, current_slot=v.on_roster_slot, available=True)
             for v in roster if v.on_roster_slot not in (Slot.IR, Slot.TAXI)]
    optimal = optimize_lineup(settings.starting_slots, cands, close_margin=0.0)
    my_starters = {a.player_id for a in optimal.assignments if a.player_id}
    current = {e.player_id for e in team.starters if e.player_id}
    hurt = {index.by_id(pid).position for pid in current | my_starters
            if index.by_id(pid) and index.by_id(pid).injury_status.value in ("doubtful", "out", "ir", "pup")}
    ctx = RosterContext(settings, my_starters | current, hurt)
    apply_rules(roster, [], ctx)
    return ctx


def player_row(v: Valued, extra: dict | None = None) -> dict:
    pl = v.player
    d = {"id": pl.id, "name": pl.name, "pos": str(pl.position), "team": pl.team or "no team", "value_ppg": v.value,
         "ros_pos_rank": pl.ros_pos_rank, "depth_chart": f"{pl.depth_position}#{pl.depth_order}" if pl.depth_order else None,
         "years_exp": pl.years_exp, "injury": str(pl.injury_status) + (f" ({pl.injury_note})" if pl.injury_note else ""),
         "recent": v.recent.line() if v.recent else None}
    return d | (extra or {})


def league_profile(ref, settings, me, week: int, hurt) -> dict:
    from ffagent.domain.models import Slot
    return {"platform": str(ref.platform), "format": str(settings.format), "teams": settings.num_teams,
            "superflex": Slot.SUPER_FLEX in settings.roster_slots, "starting_slots": [str(x) for x in settings.starting_slots],
            "bench": settings.roster_slots.get(Slot.BN, 0), "ir_slots": settings.roster_slots.get(Slot.IR, 0),
            "waivers": str(settings.waiver_type), "faab_budget": settings.faab_budget,
            "trade_deadline_week": settings.trade_deadline_week,
            "trade_deadline": settings.trade_deadline.isoformat() if settings.trade_deadline else None,
            "scoring_rec": settings.scoring.get("rec"), "scoring_pass_td": settings.scoring.get("pass_td"),
            "week": week, "my_injured_starter_positions": sorted(str(x) for x in hurt)}
