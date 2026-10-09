"""Waiver workflow (Tuesday): claims with drops and bids, reviewed, remembered, verified next run."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from ffagent.analysis.waivers import INJURY_DISCOUNT, Valued, drop_candidates, plan_claims
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
        values = deps.values.values(ref, wk, settings.scoring)
        name = lambda pid: index.by_id(pid).name if pid and index.by_id(pid) else (pid or "(empty)")

        def valued(player, slot=None, trending=0) -> Valued:
            v = values.get(player.id)
            raw = v.ppg if v else 0.0
            return Valued(player=player, value=round(raw * INJURY_DISCOUNT.get(player.injury_status, 1.0), 2), raw_value=raw,
                          source=v.source if v else "none", on_roster_slot=slot, trending=trending,
                          bye_next=bool(player.team and player.team in next_byes), on_waivers=player.on_waivers)

        roster = [valued(index.by_id(e.player_id), e.slot) for e in me.roster if e.player_id and index.by_id(e.player_id)]
        trending = dict(p.trending("add")) if hasattr(p, "trending") else {}
        usable = {pos for slot in settings.roster_slots for pos in _slot_positions(slot)}
        pool = sorted((valued(f, None, trending.get(f.id, 0)) for f in fas if f.position in usable and f.id in values),
                      key=lambda v: -v.value)[:FA_POOL]

        pending_adds = set()
        pending_lines = []
        for t in p.transactions(ref, week) + (p.transactions(ref, wk) if wk != week else []):
            if t.status == "pending" and ref.my_team_id in t.team_ids:
                pending_adds |= set(t.adds)
                pending_lines += [f"{name(a)}" + (f" (bid {t.faab_bid})" if t.faab_bid else "") for a in t.adds]

        budget = me.faab_remaining if settings.waiver_type is WaiverType.FAAB else None
        rivals = [t.faab_remaining for t in teams if t.id != me.id and t.faab_remaining is not None]
        claims = plan_claims(settings, roster, pool, budget, rivals, max_claims=max_claims, pending_adds=pending_adds)

        rejected = deps.store.rejected_fingerprints(ref.key, wk) if deps.store is not None else set()
        proposals = []
        for n, c in enumerate(claims, 1):
            add, drop = c.add.player, c.drop.player if c.drop else None
            if budget is not None:
                how = f"bid {c.bid} of {budget} FAAB"
            elif c.use_priority:
                how = f"waiver claim (priority {me.waiver_priority})"
            elif c.add.on_waivers is False:
                how = "free-agent pickup now (no priority spent)"
            else:
                how = "free-agent pickup after waivers clear (no priority spent)"
            step = f"{how}: add {add.name}" + (f", drop {drop.name}" if drop else "")
            payload = {"slot": None, "player_in": add.id, "player_in_name": add.name, "player_out": drop.id if drop else None,
                       "player_out_name": drop.name if drop else None, "bid": c.bid, "use_priority": c.use_priority,
                       "gain": c.gain, "role": c.role, "step": step}
            if fingerprint("waiver_claim", payload) in rejected:
                continue
            ev = [f"{add.name} {c.add.value} ppg ROS [{c.add.source}] ({add.position}, {add.team}){' — on bye next week' if c.add.bye_next else ''}"]
            if drop:
                ev.append(f"{drop.name} {c.drop.value} ppg ROS [{c.drop.source}]" + (f", {drop.injury_status}" if drop.injury_status.value != "healthy" else ""))
            if c.displaces:
                ev.append(f"would start over {name(c.displaces)}")
            ev += c.notes
            proposals.append(ProposedAction(
                id=f"{thread_id(ref, wk)}:{n}", kind="waiver_claim", league_key=ref.key, payload=payload,
                rationale=f"{'Claim' if c.bid is not None or c.use_priority else 'Pick up'} {add.name} ({c.role}, +{c.gain} ppg)"
                          + (f" for {drop.name}" if drop else ""), evidence=ev,
            ).model_dump(mode="json"))

        drops = [f"{v.player.name} ({v.value} ppg)" for v in drop_candidates(settings, roster)[:3]]
        verify = []
        if deps.store is not None:
            mine = me.player_ids
            for pl in deps.store.approved_payloads(ref.key, wk, "waivers"):
                verify.append(f"{pl['player_in_name']}: {'on roster' if pl['player_in'] in mine else 'not on roster (claim lost or not placed)'}")
        summary = {"team": me.name, "week": wk, "budget": budget, "priority": me.waiver_priority,
                   "waiver_type": settings.waiver_type, "values": deps.values.line(),
                   "drop_candidates": drops, "pending": pending_lines, "verify": verify,
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
