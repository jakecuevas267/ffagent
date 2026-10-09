"""Command line entry point. v1 is run manually: `ffagent leagues`, later `ffagent run <workflow>`."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC

from ffagent.config import Config, load_config
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueRef, Platform
from ffagent.providers.base import AuthExpired


def build_providers(config: Config) -> dict[str, object]:
    """Real providers, keyed by platform. Tests swap this out."""
    from ffagent.providers.sleeper import SleeperClient, SleeperProvider

    providers: dict[str, object] = {}
    platforms = {lc.platform for lc in config.leagues}
    if Platform.SLEEPER in platforms:
        providers["sleeper"] = SleeperProvider(SleeperClient())
    if Platform.ESPN in platforms:
        from ffagent.providers.espn import ESPNProvider
        providers["espn"] = ESPNProvider.from_env(config.season)
    return providers


def resolve_refs(config: Config, providers: dict[str, object]) -> list[LeagueRef]:
    refs = []
    for lc in config.leagues:
        ref = lc.to_ref(config.season)
        p = providers[lc.platform]
        if ref.my_team_id is None and lc.me:
            ref.my_team_id = p.find_my_team(ref, lc.me)
        if not ref.name:
            ref.name = p.league_name(ref)
        refs.append(ref)
    return refs


def _entry(index: PlayerIndex, e) -> dict:
    pl = index.by_id(e.player_id) if e.player_id else None
    return {"slot": e.slot, "player_id": e.player_id,
            "name": pl.name if pl else ("(empty)" if e.is_empty else e.player_id),
            "pos": pl.position if pl else None, "team": pl.team if pl else None,
            "injury": pl.injury_status if pl else None}


def cmd_leagues(config: Config, args) -> int:
    providers = build_providers(config)
    out = []
    for ref in resolve_refs(config, providers):
        p = providers[ref.platform]
        try:
            settings, teams = p.settings(ref), p.teams(ref)
        except AuthExpired as e:
            print(f"{ref.name or ref.key}: {e}", file=sys.stderr)
            continue
        index = PlayerIndex(p.players())
        teams_out = []
        for t in teams:
            teams_out.append({"id": t.id, "name": t.name, "owner": t.owner, "record": f"{t.wins}-{t.losses}",
                              "faab_remaining": t.faab_remaining, "waiver_priority": t.waiver_priority,
                              "mine": t.id == ref.my_team_id,
                              "starters": [_entry(index, e) for e in t.starters],
                              "bench": [_entry(index, e) for e in t.roster if not e.is_starter]})
        out.append({"league": ref.model_dump(mode="json"),
                    "settings": settings.model_dump(mode="json"), "teams": teams_out})

    if args.json:
        print(json.dumps(out, indent=1, default=str))
        return 0
    for lg in out:
        role = "" if lg["league"]["my_team_id"] else " — no team of mine here (commissioner view)"
        print(f"\n== {lg['league']['name']} ({lg['league']['platform']} {lg['league']['league_id']}) "
              f"{lg['settings']['num_teams']} teams, {lg['settings']['waiver_type']} waivers{role}")
        for t in lg["teams"]:
            marker = "*" if t["mine"] else " "
            budget = f"FAAB {t['faab_remaining']}" if t["faab_remaining"] is not None else f"priority {t['waiver_priority']}"
            print(f"{marker} {t['name']} ({t['owner']}) {t['record']} {budget}")
            if t["mine"] or args.all:
                for e in t["starters"]:
                    inj = f" [{e['injury']}]" if e["injury"] not in (None, "healthy") else ""
                    print(f"    {e['slot']:<10} {e['name']} {e['pos'] or ''} {e['team'] or ''}{inj}")
                for e in t["bench"]:
                    inj = f" [{e['injury']}]" if e["injury"] not in (None, "healthy") else ""
                    print(f"    {e['slot']:<10} {e['name']} {e['pos'] or ''} {e['team'] or ''}{inj}")
    return 0


def build_lineup_deps(config: Config, providers: dict[str, object], store=None):
    """Real projections + schedule for the graphs. Tests swap this out."""
    from ffagent.graphs.lineup import LineupDeps
    from ffagent.providers.espn import ESPNProjections
    from ffagent.schedule.nfl import fetch_week
    from ffagent.sources.projections import SleeperProjections

    deps = {}
    for plat, p in providers.items():
        proj = ESPNProjections(p) if plat == "espn" else SleeperProjections()
        deps[plat] = LineupDeps(p, proj, fetch_week, store=store)
    return deps


def _select_refs(config: Config, providers, league: str | None) -> list[LeagueRef] | None:
    refs = resolve_refs(config, providers)
    if league:
        refs = [r for r in refs if league.lower() in (r.name.lower(), r.league_id)]
        if not refs:
            print(f"no league matches {league!r}", file=sys.stderr)
            return None
    return refs


def _week_for(deps, ref, override: int | None) -> int:
    deps.provider.league_name(ref)  # ensures the league is loaded (ESPN reads the week from it)
    return override or deps.provider.current_week()


def _now():
    from datetime import datetime
    return datetime.now(UTC)


def _sleep(seconds: float) -> None:
    import time
    time.sleep(seconds)


def cmd_run_lineup(config: Config, args) -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from ffagent.graphs.lineup import build_lineup_graph, thread_id
    from ffagent.review.cli import collect_decisions, print_checklist, print_review
    from ffagent.store.db import Store

    providers = build_providers(config)
    tz = ZoneInfo(config.timezone)
    store = None if args.dry_run else Store(config.data_dir / "ffagent.sqlite")
    deps_by_platform = build_lineup_deps(config, providers, store)
    refs = _select_refs(config, providers, args.league)
    if refs is None:
        return 2
    now = datetime.fromisoformat(args.now) if args.now else _now()
    saver = MemorySaver() if args.dry_run else store.checkpointer()

    for ref in refs:
        deps = deps_by_platform[ref.platform]
        week = _week_for(deps, ref, args.week)
        graph = build_lineup_graph(deps, saver)
        cfg = {"configurable": {"thread_id": thread_id(ref, week)}}
        prior = graph.get_state(cfg)
        if prior.values and not prior.next and not args.again:
            print(f"\n== {ref.name}: already reviewed for week {week}; use --again to redo")
            print_checklist(ref.name, prior.values.get("checklist", []))
            continue
        if prior.next:
            out = {"__interrupt__": prior.tasks[0].interrupts} if prior.tasks and prior.tasks[0].interrupts else None
        else:
            out = None
        if out is None:
            try:
                out = graph.invoke({"league": ref.model_dump(mode="json"), "week": week, "now": now.isoformat()}, cfg)
            except AuthExpired as e:
                print(f"{ref.name}: {e}", file=sys.stderr)
                continue
        if out.get("error"):
            print(f"\n== {ref.name}: {out['error']}")
            continue
        if "__interrupt__" not in out:
            s = out["summary"]
            print(f"\n== {ref.name} — week {week} — {s['team']}: lineup already optimal ({s['projected_after']} projected)")
            for f in s.get("flags", []):
                print(f"   ? {f}")
            for line in s.get("pending", []):
                print(f"   ! still to do from an earlier review: {line}")
            continue
        payload = out["__interrupt__"][0].value
        print_review(payload, tz=tz)
        if args.dry_run:
            continue
        decisions = collect_decisions(payload["proposals"], auto_approve=args.auto_approve)
        final = graph.invoke(Command(resume=decisions), cfg)
        print_checklist(ref.name, final.get("checklist", []))
    return 0


def cmd_run_injury(config: Config, args) -> int:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from ffagent.graphs.injury import build_injury_graph, thread_id
    from ffagent.review.cli import collect_decisions, localize, print_checklist, print_review
    from ffagent.store.db import Store

    providers = build_providers(config)
    tz = ZoneInfo(config.timezone)
    store = None if args.dry_run else Store(config.data_dir / "ffagent.sqlite")
    deps_by_platform = build_lineup_deps(config, providers, store)
    refs = _select_refs(config, providers, args.league)
    if refs is None:
        return 2
    saver = MemorySaver() if args.dry_run else store.checkpointer()

    def one_pass(now: datetime) -> datetime | None:
        """Run every league once; return the last kickoff still ahead today (for --watch), else None."""
        last_ahead = None
        for ref in refs:
            deps = deps_by_platform[ref.platform]
            if hasattr(deps.provider, "refresh"):
                deps.provider.refresh()  # injuries change by the minute on game day
            week = _week_for(deps, ref, args.week)
            graph = build_injury_graph(deps, saver)
            cfg = {"configurable": {"thread_id": thread_id(ref, week, now)}}
            try:
                out = graph.invoke({"league": ref.model_dump(mode="json"), "week": week, "now": now.isoformat()}, cfg)
            except AuthExpired as e:
                print(f"{ref.name}: {e}", file=sys.stderr)
                continue
            if out.get("error"):
                print(f"\n== {ref.name}: {out['error']}")
                continue
            s = out["summary"]
            sched = deps.schedule_for_week(ref.season, week)
            ahead = [g.kickoff for g in sched.games
                     if g.kickoff > now and g.kickoff.astimezone(now.tzinfo).date() == now.date()]
            if ahead:
                last_ahead = max(last_ahead or max(ahead), max(ahead))
            quiet = "__interrupt__" not in out
            header = f"\n== {ref.name} — week {week} — {s['team']} — {now.astimezone(tz).strftime('%a %H:%M %Z')}"
            if quiet:
                print(header + ": no injury moves needed")
            for line in s.get("pending", []):
                print(f"   ! still to do from the lineup review: {line}")
            for c in s.get("changes", []):
                print(f"   Δ {c}")
            for w in s.get("warnings", []):
                print(f"   ! {w}")
            for w in s.get("watch", []):
                print(f"   ? watch: {localize(w, tz)}")
            if quiet:
                continue
            payload = out["__interrupt__"][0].value
            print_review(payload, tz=tz)
            if args.dry_run:
                continue
            decisions = collect_decisions(payload["proposals"], auto_approve=args.auto_approve)
            final = graph.invoke(Command(resume=decisions), cfg)
            print_checklist(ref.name, final.get("checklist", []))
        return last_ahead

    now = datetime.fromisoformat(args.now) if args.now else _now()
    last = one_pass(now)
    if not args.watch:
        return 0
    interval = timedelta(minutes=args.interval)
    while last is not None and now < last + timedelta(minutes=5):
        _sleep(interval.total_seconds())
        now = (now + interval) if args.now else _now()
        last = one_pass(now) or last
    print("\nwatch finished: no more kickoffs today")
    return 0


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv()  # ESPN cookies, LLM keys; never read from ffagent.yaml
    ap = argparse.ArgumentParser(prog="ffagent")
    ap.add_argument("--config", default="ffagent.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("leagues", help="print normalized leagues and rosters")
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--all", action="store_true", help="show every team's roster, not just mine")
    run = sub.add_parser("run", help="run a workflow now")
    run.add_argument("workflow", choices=["lineup", "injury"])
    run.add_argument("--league", help="league name or id; default all")
    run.add_argument("--week", type=int)
    run.add_argument("--now", help="ISO timestamp override (testing)")
    run.add_argument("--dry-run", action="store_true", help="show proposals, do not ask or record")
    run.add_argument("--auto-approve", action="store_true")
    run.add_argument("--again", action="store_true", help="redo a league already reviewed this week")
    run.add_argument("--watch", action="store_true", help="injury: keep checking until the day's last kickoff")
    run.add_argument("--interval", type=int, default=15, help="injury --watch: minutes between checks")
    args = ap.parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2
    if args.cmd == "leagues":
        return cmd_leagues(config, args)
    if args.cmd == "run" and args.workflow == "lineup":
        return cmd_run_lineup(config, args)
    if args.cmd == "run" and args.workflow == "injury":
        return cmd_run_injury(config, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
