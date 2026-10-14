"""Waiver workflow (Tuesday): claims with drops and bids, reviewed, remembered, verified next run."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from ffagent.analysis.lineup import Candidate, optimize_lineup
from ffagent.analysis.rules import RosterContext
from ffagent.analysis.waivers import (
    INJURY_DISCOUNT,
    Valued,
    apply_rules,
    drop_candidates,
    plan_claims,
    replacement_levels,
    speculative_adds,
)
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueRef, Platform, ProposedAction, Slot, WaiverType
from ffagent.graphs.common import ReviewState, build_review_graph
from ffagent.graphs.lineup import LineupDeps
from ffagent.store.db import fingerprint

FA_POOL = 80  # free agents considered, by value


def thread_id(ref: LeagueRef, week: int) -> str:
    return f"waivers:{ref.key}:{ref.season}:{week}"


def target_week(schedule_for_week, season: int, week: int, now: datetime) -> int:
    """Waivers prepare the upcoming week: once this week's last game is over, that is week + 1."""
    sched = schedule_for_week(season, week)
    return week + 1 if now > sched.last_kickoff + timedelta(hours=4) else week


def build_waiver_graph(deps: LineupDeps, checkpointer: BaseCheckpointSaver | None = None, max_claims: int = 3):
    def analyze(state: ReviewState) -> dict[str, Any]:
        ref = LeagueRef.model_validate(state["league"])
        week = state["week"]
        now = datetime.fromisoformat(state["now"]).astimezone(UTC)
        if ref.my_team_id is None:
            return {"error": "no team of mine in this league (commissioner view); waiver workflow skipped", "proposals": []}
        p = deps.provider
        settings = p.settings(ref)
        teams = p.teams(ref)
        me = next(t for t in teams if t.id == ref.my_team_id)
        wk = target_week(deps.schedule_for_week, ref.season, week, now)
        next_byes = deps.schedule_for_week(ref.season, wk).teams_on_bye

        fas = p.free_agents(ref, week=wk) if ref.platform is Platform.ESPN else p.free_agents(ref)
        index = PlayerIndex(p.players())
        if ref.platform is Platform.ESPN and deps.nfl is not None:
            from ffagent.providers.sleeper import enrich_from_sleeper
            enrich_from_sleeper(list(index) + fas, PlayerIndex(deps.nfl.players()))
        expert = getattr(deps.values, "expert", None)
        ranks = {}
        if expert is not None and hasattr(expert, "ros_ranks"):
            try:
                ranks = expert.ros_ranks(ref)
            except Exception:  # noqa: BLE001 - ranks are a refinement, never a blocker
                ranks = {}
        for pl in list(index) + fas:
            if pl.id in ranks:
                pl.ros_pos_rank = ranks[pl.id]
        values = deps.values.values(ref, wk, settings.scoring)
        name = lambda pid: index.by_id(pid).name if pid and index.by_id(pid) else (pid or "(empty)")

        # Recent performance (Sleeper ids; other platforms map through the crosswalk).
        recent: dict = {}
        if deps.stats is not None:
            try:
                raw_recent = deps.stats.recent(ref.season, max(1, wk - 1), settings.scoring)
            except Exception:  # noqa: BLE001 - a refinement, never a blocker
                raw_recent = {}
            if ref.platform is Platform.SLEEPER:
                recent = raw_recent
            elif deps.nfl is not None:
                sidx = PlayerIndex(deps.nfl.players())
                for pl in list(index) + fas:
                    eid = pl.external_ids.get(Platform.ESPN)
                    sp = sidx.by_external(Platform.ESPN, eid) if eid else None
                    if sp is not None and sp.id in raw_recent:
                        recent[pl.id] = raw_recent[sp.id]

        def valued(player, slot=None, trending=0) -> Valued:
            v = values.get(player.id)
            raw = v.ppg if v else 0.0
            return Valued(player=player, value=round(raw * INJURY_DISCOUNT.get(player.injury_status, 1.0), 2), raw_value=raw,
                          source=v.source if v else "none", on_roster_slot=slot, trending=trending,
                          bye_next=bool(player.team and player.team in next_byes), on_waivers=player.on_waivers,
                          recent=recent.get(player.id))

        roster = [valued(index.by_id(e.player_id), e.slot) for e in me.roster if e.player_id and index.by_id(e.player_id)]
        trends = deps.trends.for_league("add", ref, index) if deps.trends is not None else []
        trend_count = {t.player_id: t.count for t in trends}
        usable = {pos for slot in settings.roster_slots for pos in _slot_positions(slot)}
        available = {f.id: valued(f, None, trend_count.get(f.id, 0)) for f in fas if f.position in usable}
        pool = sorted((v for v in available.values() if v.value > 0), key=lambda v: -v.value)[:FA_POOL]

        pending_adds = set()
        pending_lines = []
        for t in p.transactions(ref, week) + (p.transactions(ref, wk) if wk != week else []):
            if t.status == "pending" and ref.my_team_id in t.team_ids:
                pending_adds |= set(t.adds)
                pending_lines += [f"{name(a)}" + (f" (bid {t.faab_bid})" if t.faab_bid else "") for a in t.adds]

        # Roster context for the playbook rules: who starts for me (by value), where I am hurt.
        cands = [Candidate(player=v.player, points=v.value, current_slot=v.on_roster_slot, available=True)
                 for v in roster if v.on_roster_slot not in (Slot.IR, Slot.TAXI)]
        optimal = optimize_lineup(settings.starting_slots, cands, close_margin=0.0)
        my_starters = {a.player_id for a in optimal.assignments if a.player_id}
        current_starters = {e.player_id for e in me.starters if e.player_id}
        hurt = {index.by_id(pid).position for pid in current_starters | my_starters
                if index.by_id(pid) and index.by_id(pid).injury_status.value in ("doubtful", "out", "ir", "pup")}
        ctx = RosterContext(settings, my_starters | current_starters, hurt)  # what I start now counts too
        all_players = list(index)
        apply_rules(roster, list(available.values()), ctx,
                    teammates_of=lambda pl: [x for x in all_players if x.team == pl.team and x.position == pl.position and x.id != pl.id])

        budget = me.faab_remaining if settings.waiver_type is WaiverType.FAAB else None
        rivals = [t.faab_remaining for t in teams if t.id != me.id and t.faab_remaining is not None]
        claims = plan_claims(settings, roster, pool, budget, rivals, max_claims=max_claims, pending_adds=pending_adds, ctx=ctx)
        claims += speculative_adds(trends, available, roster, settings, budget, replacement_levels(pool), max_adds=2,
                                   pending_adds=pending_adds, already={c.add.player.id for c in claims})

        rejected = deps.store.rejected_fingerprints(ref.key, wk) if deps.store is not None else set()
        proposals = []
        advice_line, watch_lines, questioned = None, [], []
        advised = deps.adviser is not None and deps.adviser.enabled
        if advised:
            by_id = {v.player.id: v for v in roster} | {v.player.id: v for v in available.values()}
            cand_ids = ({c.add.player.id for c in claims} | {t.player_id for t in trends[:10] if t.player_id in available}
                        | {v.player.id for v in pool[:15]})
            droppable = [v for v in roster if v.on_roster_slot not in (Slot.IR, Slot.TAXI) and not v.holds
                         and v.droppable and v.player.droppable]
            swap_ok = [v for v in roster if v.holds and all(h.startswith("fantasy-starter hold") for h in v.holds)]
            det = {c.add.player.id: c for c in claims}

            def row(v: Valued, extra: dict | None = None) -> dict:
                pl = v.player
                d = {"id": pl.id, "name": pl.name, "pos": str(pl.position), "team": pl.team or "no team",
                     "value_ppg": v.value, "ros_pos_rank": pl.ros_pos_rank,
                     "depth_chart": f"{pl.depth_position}#{pl.depth_order}" if pl.depth_order else None,
                     "years_exp": pl.years_exp,
                     "injury": str(pl.injury_status) + (f" ({pl.injury_note})" if pl.injury_note else ""),
                     "recent": v.recent.line() if v.recent else None, "trending_adds_24h": v.trending or None,
                     "bye_next_week": v.bye_next or None}
                return d | (extra or {})

            roster_rows = [row(v, {"slot": str(v.on_roster_slot), "holds": v.holds, "potential": v.potential,
                                   "construction": v.construction}) for v in roster]
            cand_rows = []
            for pid in cand_ids:
                v, c = by_id[pid], det.get(pid)
                cand_rows.append(row(v, {"flags": v.flags, "on_waivers": v.on_waivers, "deterministic": (
                    {"role": c.role, "gain_ppg": c.gain, "suggested_drop": c.drop.player.name if c.drop else None,
                     "bid": c.bid, "notes": c.notes} if c else None)}))
            drop_rows = [row(v, {"slot": str(v.on_roster_slot), "potential": v.potential, "construction": v.construction})
                         for v in droppable]
            drop_rows += [row(v, {"slot": str(v.on_roster_slot), "only_for_same_position_upgrade": True}) for v in swap_ok]
            profile = {"platform": str(ref.platform), "format": str(settings.format), "teams": settings.num_teams,
                       "superflex": Slot.SUPER_FLEX in settings.roster_slots,
                       "starting_slots": [str(x) for x in settings.starting_slots],
                       "bench": settings.roster_slots.get(Slot.BN, 0), "ir_slots": settings.roster_slots.get(Slot.IR, 0),
                       "ir_allows": sorted(str(x) for x in settings.ir_statuses), "waivers": str(settings.waiver_type),
                       "faab_budget": settings.faab_budget, "my_priority": me.waiver_priority,
                       "scoring_rec": settings.scoring.get("rec"), "scoring_pass_td": settings.scoring.get("pass_td"),
                       "week": wk, "my_injured_starter_positions": sorted(str(x) for x in hurt)}
            decisions = deps.store.recent_decisions(ref.key) if deps.store is not None else []
            res = deps.adviser.advise(profile, roster_rows, cand_rows, drop_rows, decisions, budget, max_claims)
            if res.error:
                advice_line = f"advise: {res.error}; showing the deterministic proposals"
                advised = False
            else:
                advice_line = f"advise: {res.model} chose {len(res.proposals)} of {len(cand_rows)} candidates"
                if res.rejected:
                    advice_line += f"; dropped by validation: {'; '.join(res.rejected)}"
                watch_lines, questioned = list(res.watch), list(res.holds_questioned)
                for n, a in enumerate(res.proposals, 1):
                    add, drop = by_id[a.add_id], by_id[a.drop_id]
                    if budget is not None:
                        how = f"bid {a.bid} of {budget} FAAB"
                    elif a.action == "claim":
                        how = f"waiver claim (priority {me.waiver_priority})"
                    elif add.on_waivers is False:
                        how = "free-agent pickup now (no priority spent)"
                    else:
                        how = "free-agent pickup after waivers clear (no priority spent)"
                    payload = {"slot": None, "player_in": add.player.id, "player_in_name": add.player.name,
                               "player_out": drop.player.id, "player_out_name": drop.player.name, "bid": a.bid,
                               "use_priority": a.action == "claim" and budget is None,
                               "gain": det[add.player.id].gain if add.player.id in det else None,
                               "role": a.action, "principle": a.principle,
                               "step": f"{how}: add {add.player.name}, drop {drop.player.name}"}
                    if fingerprint("waiver_claim", payload) in rejected:
                        continue
                    ev = [f"{add.player.name} {add.value} ppg ROS [{add.source}] ({add.player.position}, {add.player.team or 'no team'})"
                          + (f"; {add.recent.line()}" if add.recent else "") + (f"; {', '.join(add.flags)}" if add.flags else ""),
                          f"{drop.player.name} {drop.value} ppg ROS [{drop.source}]" + (f"; {drop.recent.line()}" if drop.recent else "")
                          + (f" — {drop.construction}" if drop.construction else "")]
                    proposals.append(ProposedAction(
                        id=f"{thread_id(ref, wk)}:a{n}", kind="waiver_claim", league_key=ref.key, payload=payload,
                        rationale=f"{a.rationale} [{a.principle}]", evidence=ev).model_dump(mode="json"))
        for n, c in enumerate([] if advised else claims, 1):
            add, drop = c.add.player, c.drop.player if c.drop else None
            if budget is not None:
                how = f"bid {c.bid} of {budget} FAAB"
            elif c.use_priority:
                how = f"waiver claim (priority {me.waiver_priority})"
            elif c.add.on_waivers is False:
                how = "free-agent pickup now (no priority spent)"
            else:
                how = "free-agent pickup after waivers clear (no priority spent)"
            if c.role == "speculative":
                how = f"bid {c.bid} of {budget} FAAB (flier)" if c.bid is not None else "free-agent pickup (flier, no priority spent)"
            step = f"{how}: add {add.name}" + (f", drop {drop.name}" if drop else "")
            payload = {"slot": None, "player_in": add.id, "player_in_name": add.name, "player_out": drop.id if drop else None,
                       "player_out_name": drop.name if drop else None, "bid": c.bid, "use_priority": c.use_priority,
                       "gain": c.gain, "role": c.role, "step": step}
            if fingerprint("waiver_claim", payload) in rejected:
                continue
            ev = [f"{add.name} {c.add.value} ppg ROS [{c.add.source}] ({add.position}, {add.team or 'no team'}){' — on bye next week' if c.add.bye_next else ''}"]
            if drop:
                why_drop = c.drop.construction or ("cheapest unheld player" if not c.drop.potential else "note: " + "; ".join(c.drop.potential))
                ev.append(f"{drop.name} {c.drop.value} ppg ROS [{c.drop.source}]" + (f", {drop.injury_status}" if drop.injury_status.value != "healthy" else "") + f" — {why_drop}")
            if c.displaces:
                ev.append(f"would start over {name(c.displaces)}")
            ev += c.notes
            proposals.append(ProposedAction(
                id=f"{thread_id(ref, wk)}:{n}", kind="waiver_claim", league_key=ref.key, payload=payload,
                rationale=(f"Stash {add.name} (speculative: {c.notes[0]})" if c.role == "speculative" else
                           f"{'Claim' if c.bid is not None or c.use_priority else 'Pick up'} {add.name} ({c.role}, +{c.gain} ppg)")
                          + (f" for {drop.name}" if drop else ""), evidence=ev,
            ).model_dump(mode="json"))

        drops = [f"{v.player.name} ({v.value} ppg" + (f", {v.construction}" if v.construction else "") + ")"
                 for v in drop_candidates(settings, roster)[:3]]
        holds = [f"{v.player.name}: {v.holds[0]}" for v in sorted(roster, key=lambda v: v.value) if v.holds][:4]
        trending_lines = []
        for t in trends:
            v = available.get(t.player_id)
            if v is None:
                continue
            pl = v.player
            ctx = pl.team or "no team"
            if pl.injury_status.value != "healthy":
                ctx += f", {pl.injury_status}"
            trending_lines.append(f"{pl.name} ({pl.position}, {ctx}) {v.value} ppg — {t.label}")
            if len(trending_lines) >= 5:
                break
        mine = me.player_ids
        trending_drops = []
        if deps.trends is not None:
            for t in deps.trends.for_league("drop", ref, index):
                if t.player_id in mine:
                    trending_drops.append(f"{name(t.player_id)} — {t.label}")
        verify = []
        if deps.store is not None:
            for pl in deps.store.approved_payloads(ref.key, wk, "waivers"):
                verify.append(f"{pl['player_in_name']}: {'on roster' if pl['player_in'] in mine else 'not on roster (claim lost or not placed)'}")
        summary = {"team": me.name, "week": wk, "budget": budget, "priority": me.waiver_priority,
                   "advise": advice_line, "watch": watch_lines, "holds_questioned": questioned,
                   "waiver_type": settings.waiver_type, "values": deps.values.line(),
                   "drop_candidates": drops, "pending": pending_lines, "verify": verify,
                   "trending": trending_lines, "trending_drops": trending_drops, "holds": holds,
                   "hurt_positions": sorted(str(x) for x in hurt),
                   "projected_before": 0, "projected_after": 0, "first_kickoff": deps.schedule_for_week(ref.season, wk).first_kickoff.isoformat(),
                   "warnings": [], "flags": [], "close_calls": []}
        return {"proposals": proposals, "summary": summary, "week": wk}

    def record(state: ReviewState) -> dict[str, Any]:
        if deps.store is not None and state.get("proposals"):
            ref = LeagueRef.model_validate(state["league"])
            deps.store.record_decisions(ref.key, state["week"], "waivers", state["proposals"], state.get("decisions", []),
                                        now=datetime.fromisoformat(state["now"]))
        return {}

    return build_review_graph(analyze, checkpointer, record)


def _slot_positions(slot: Slot):
    from ffagent.domain.models import Position, slot_accepts
    return [pos for pos in Position if slot_accepts(slot, pos)] if slot not in (Slot.BN, Slot.IR, Slot.TAXI) else []
