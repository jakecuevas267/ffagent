"""Trade targets: every roster valued, complementary partners, offers that help both sides, advised."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

from ffagent.analysis.trades import SKILL, find_offers, league_medians, profile
from ffagent.analysis.waivers import replacement_levels
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueRef, Platform, ProposedAction
from ffagent.graphs.common import ReviewState, build_review_graph
from ffagent.graphs.facts import (
    context_for,
    enrich,
    league_profile,
    make_valued,
    player_row,
    recent_performance,
    ros_ranks,
    roster_valued,
)
from ffagent.graphs.lineup import LineupDeps
from ffagent.graphs.waivers import target_week
from ffagent.store.db import fingerprint


def thread_id(ref: LeagueRef, week: int) -> str:
    return f"trades:{ref.key}:{ref.season}:{week}"


def past_deadline(settings, week: int, now: datetime) -> str | None:
    if settings.trade_deadline is not None and now > settings.trade_deadline:
        return f"trade deadline passed ({settings.trade_deadline.date()})"
    if settings.trade_deadline_week and week > settings.trade_deadline_week:
        return f"trade deadline passed (week {settings.trade_deadline_week})"
    return None


def default_message(partner_name: str, give_names: list[str], get_names: list[str], their_need, my_need) -> str:
    return (f"Hey {partner_name} — would you do {' + '.join(give_names)} for {' + '.join(get_names)}? "
            f"You look thin at {their_need} and I could use help at {my_need}. Happy to talk it through.")


def build_trade_graph(deps: LineupDeps, checkpointer: BaseCheckpointSaver | None = None, max_offers: int = 3,
                      two_for_one: bool = True, adviser=None):
    def analyze(state: ReviewState) -> dict[str, Any]:
        ref = LeagueRef.model_validate(state["league"])
        week = state["week"]
        now = datetime.fromisoformat(state["now"]).astimezone(UTC)
        if ref.my_team_id is None:
            return {"error": "no team of mine in this league (commissioner view); trade workflow skipped", "proposals": []}
        p = deps.provider
        settings = p.settings(ref)
        wk = target_week(deps.schedule_for_week, ref.season, week, now)
        if (why := past_deadline(settings, wk, now)):
            return {"error": why, "proposals": []}
        teams = p.teams(ref)
        me = next(t for t in teams if t.id == ref.my_team_id)
        fas = p.free_agents(ref, week=wk) if ref.platform is Platform.ESPN else p.free_agents(ref)
        index = PlayerIndex(p.players())
        enrich(deps, ref, index, fas)
        ranks = ros_ranks(deps, ref)
        for pl in list(index) + fas:
            if pl.id in ranks:
                pl.ros_pos_rank = ranks[pl.id]
        values = deps.values.values(ref, wk, settings.scoring)
        recent = recent_performance(deps, ref, index, fas, wk - 1, settings.scoring)
        next_byes = deps.schedule_for_week(ref.season, wk).teams_on_bye

        rosters = {t.id: roster_valued(t, index, values, recent, next_byes) for t in teams}
        ctx = context_for(settings, me, rosters[me.id], index)
        profiles = {t.id: profile(t.id, t.name, rosters[t.id], settings) for t in teams}
        med = league_medians(list(profiles.values()))
        usable = {pos for pos in SKILL}
        pool = sorted((make_valued(f, values, recent, next_byes) for f in fas if f.position in usable and f.id in values),
                      key=lambda v: -v.value)[:80]
        replacement = replacement_levels(pool)
        mine = profiles[me.id]
        others = [pr for tid, pr in profiles.items() if tid != me.id]
        offers = find_offers(mine, others, settings, med, replacement, max_offers=max_offers * 2, allow_two_for_one=two_for_one)

        rejected = deps.store.rejected_fingerprints(ref.key, wk) if deps.store is not None else set()

        def offer_row(o) -> dict:
            return {"id": o.id, "partner": o.partner.name, "give": [player_row(v) for v in o.give],
                    "get": [player_row(v) for v in o.get], "my_lineup_gain_ppg": o.my_gain, "their_lineup_gain_ppg": o.their_gain,
                    "my_need": str(o.my_need), "their_need": str(o.their_need), "notes": o.notes}

        advice_line, watch, chosen = None, [], None
        if adviser is not None and adviser.enabled and offers:
            prof = league_profile(ref, settings, me, wk, ctx.injured_starter_positions)
            my_rows = [player_row(v, {"slot": str(v.on_roster_slot), "holds": v.holds, "potential": v.potential}) for v in rosters[me.id]]
            decisions = deps.store.recent_decisions(ref.key) if deps.store is not None else []
            res = adviser.advise(prof, my_rows, [offer_row(o) for o in offers], decisions, max_offers)
            if res.error:
                advice_line = f"advise: {res.error}; showing the deterministic offers"
            else:
                advice_line = f"advise: {res.model} chose {len(res.proposals)} of {len(offers)} offers"
                if res.rejected:
                    advice_line += f"; dropped by validation: {'; '.join(res.rejected)}"
                watch = list(res.watch)
                by_id = {o.id: o for o in offers}
                chosen = [(by_id[a.offer_id], a) for a in res.proposals]
        if chosen is None:
            chosen = [(o, None) for o in offers[:max_offers]]

        proposals = []
        for n, (o, a) in enumerate(chosen, 1):
            give_names, get_names = [v.player.name for v in o.give], [v.player.name for v in o.get]
            msg = a.message if a is not None else default_message(o.partner.name, give_names, get_names, o.their_need, o.my_need)
            payload = {"slot": None, "player_in": "+".join(sorted(v.player.id for v in o.get)),
                       "player_out": "+".join(sorted(v.player.id for v in o.give)),
                       "player_in_name": " + ".join(get_names), "player_out_name": " + ".join(give_names),
                       "partner_team_id": o.partner.team_id, "partner_name": o.partner.name,
                       "give": [v.player.id for v in o.give], "get": [v.player.id for v in o.get],
                       "my_gain": o.my_gain, "their_gain": o.their_gain, "message": msg,
                       "principle": a.principle if a is not None else None,
                       "step": f"Offer {o.partner.name}: give {' + '.join(give_names)} for {' + '.join(get_names)}; send: \"{msg}\""}
            if fingerprint("trade_offer", payload) in rejected:
                continue
            ev = [f"get {v.player.name} {v.value} ppg ROS [{v.source}] ({v.player.position}, {v.player.team})"
                  + (f", {v.player.position}{v.player.ros_pos_rank} ROS" if v.player.ros_pos_rank else "")
                  + (f"; {v.recent.line()}" if v.recent else "") for v in o.get]
            ev += [f"give {v.player.name} {v.value} ppg ROS [{v.source}] ({v.player.position}, {v.player.team})"
                   + (f", {v.player.position}{v.player.ros_pos_rank} ROS" if v.player.ros_pos_rank else "")
                   + (f"; {v.recent.line()}" if v.recent else "") for v in o.give]
            ev.append(f"my lineup {o.my_gain:+} ppg, theirs {o.their_gain:+} ppg; my need {o.my_need}, their need {o.their_need}")
            ev += o.notes
            rationale = (f"{a.rationale} [{a.principle}]" if a is not None else
                         f"Trade with {o.partner.name}: {' + '.join(give_names)} for {' + '.join(get_names)} "
                         f"({o.my_gain:+} ppg for me, {o.their_gain:+} for them)")
            proposals.append(ProposedAction(id=f"{thread_id(ref, wk)}:{n}", kind="trade_offer", league_key=ref.key,
                                            payload=payload, rationale=rationale, evidence=ev).model_dump(mode="json"))

        verify = []
        if deps.store is not None:
            mine_ids = me.player_ids
            for pl in deps.store.approved_payloads(ref.key, wk, "trades"):
                done = all(g not in mine_ids for g in pl.get("give", [])) and all(g in mine_ids for g in pl.get("get", []))
                verify.append(f"{pl['step'].split('; send')[0]}: {'completed' if done else 'not completed'}")

        strengths = [f"{pos}: {mine.strength.get(pos, 0.0):.1f} vs median {med.get(pos, 0.0):.1f}"
                     + (" (need)" if mine.need(pos, med) > 0 else "") for pos in SKILL]
        partners = []
        for o in others:
            needs = [str(pos) for pos in SKILL if o.need(pos, med) > 0]
            if needs:
                partners.append(f"{o.name}: needs {', '.join(needs)}")
        summary = {"team": me.name, "week": wk, "strengths": strengths, "partners": partners[:8],
                   "deadline": (settings.trade_deadline.date().isoformat() if settings.trade_deadline
                                else f"week {settings.trade_deadline_week}" if settings.trade_deadline_week else "none"),
                   "values": deps.values.line(), "advise": advice_line, "watch": watch, "verify": verify,
                   "offers_found": len(offers), "projected_before": 0, "projected_after": 0,
                   "first_kickoff": deps.schedule_for_week(ref.season, wk).first_kickoff.isoformat(),
                   "warnings": [], "flags": [], "close_calls": []}
        return {"proposals": proposals, "summary": summary, "week": wk}

    def record(state: ReviewState) -> dict[str, Any]:
        if deps.store is not None and state.get("proposals"):
            ref = LeagueRef.model_validate(state["league"])
            deps.store.record_decisions(ref.key, state["week"], "trades", state["proposals"], state.get("decisions", []),
                                        now=datetime.fromisoformat(state["now"]))
        return {}

    return build_review_graph(analyze, checkpointer, record)
