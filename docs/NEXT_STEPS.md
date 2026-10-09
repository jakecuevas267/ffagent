# Next steps

Roadmap from where the project is to the vision in [SPEC.md](SPEC.md). Updated 2026-10-09.

## The vision, in one paragraph

Every week, one agent handles the chores across all of the manager's leagues on both platforms: sets the
lineup before Thursday, catches injuries and inactives on every game day, prepares waiver claims for
Tuesday, surfaces trade targets, and tips off league-mates who are starting injured or bye-week
players. It reads from Sleeper and ESPN, ranks with a trusted expert source (Upper Hand), proposes
specific moves with evidence, stops for approval on each, and remembers what was decided. It runs
itself on the real NFL schedule, reaches the manager where they are (Discord), keeps credentials somewhere
safer than a file, and is open source so anyone with a Sleeper or ESPN league can use it.

## Where things stand

| Area | State |
|---|---|
| Foundation: models, player identity, config, CLI | Done |
| Sleeper provider | Done (official API, no auth) |
| ESPN provider | Done except transactions (needed for waivers and verification) |
| NFL schedule: kickoffs, byes, locks, slates | Done |
| Projections | Sleeper feed for Sleeper leagues; ESPN's own for ESPN leagues |
| Slice 1 — lineup (Thursday lock) | Done, used live on all 6 leagues |
| Slice 2 — injury start/sit + IR housekeeping | Built; first real game-day use pending (Sun 10/11) |
| Slice 3 — waivers (Tuesday) | Not started |
| Slice 4 — trade targets | Not started |
| Slice 5 — league-wide lineup audit | Not started |
| Expert source (Upper Hand) | Access requested; projections stand in meanwhile |
| Review surface | CLI only |
| Scheduling | Manual (`ffagent run …`, `--watch` for game days) |
| Credentials | `.env` |

Tests: 186 offline, 4 opt-in live. Everything through slice 1 is committed; slice 2 is not yet.

## Immediate

- [ ] Commit slice 2 (injury workflow, IR housekeeping, Sleeper IR-slot fix).
- [ ] Sunday 10/11: first real `ffagent run injury --watch`. Capture what was noisy, what was missed,
      and whether "Doubtful = bench" and "Questionable = watch" are the right defaults.
- [ ] Wednesday 10/14: first real `ffagent run lineup` review for week 6 (not a dry run), to see
      the approve/reject flow and the checklist on the phone.

## Slice 3 — Waivers (Tuesday)

What it should do: for each league, rank available players against my needs (byes, injuries,
positional weakness), pick a drop for each add, size the claim (FAB amount, or claim order for
priority leagues), and chain fallbacks so a lost claim falls through to the next.

Needs before it can be built:
- [ ] ESPN transactions view (`mTransactions2` / recent activity) so claims and adds can be verified.
- [ ] Sleeper trending adds/drops (endpoint exists, not wired).
- [ ] Each league's waiver processing time: Sleeper `waiver_day_of_week` + `daily_waivers`;
      ESPN `waiverProcessDays` + `waiverProcessHour`. Both are in the fixtures.
- [ ] Rest-of-season value, not just this week's projection, to judge drops. Without an expert source
      the fallback is season-to-date points plus next-week projection.

Decisions for the manager:
- How aggressive on FAAB by default in FAAB leagues? A percentage of remaining
  budget scaled by the expert's recommendation, capped by what rivals can bid, is the plan.
- For rolling/priority leagues, should the agent suggest spending priority at all on marginal adds?

## Slice 4 — Trade targets

Score each team's positional strength from rest-of-season values against the league median, treat
injuries as needs on both sides, match my surplus to their need, and draft offers inside a fairness
band with a message to send.

- [ ] Needs rest-of-season rankings or trade values; this is the slice that most wants Upper Hand.
- [ ] Trade deadline per league is already read (date on ESPN, week on Sleeper).
- [ ] Per-manager notes in config ("never trades RBs") — cheap to add, big effect on usefulness.

## Slice 5 — League-wide lineup audit

Check every team's lineup for byes, Out/IR/Doubtful starters and empty slots; draft a short notice
per affected manager for the user to send. Also covers commissioner-only leagues,
where nothing else applies.

- [ ] Both providers already expose every team's lineup; this is mostly analysis plus a new
      `manager_notice` proposal kind.
- [ ] "Already told them this week" memory, so a manager who leaves a player in is not nagged.

## Cross-cutting

### Expert source
- [ ] The publisher's answer on Upper Hand access (sanctioned export or endpoint). Then an `UpperHandSource`.
- [ ] Fallback if that stalls: a manual CSV source (`rank,name,pos,team`) dropped in `data/rankings/`,
      and optionally FantasyPros consensus (official API, key on request).
- [ ] Freshness gate: a workflow that needs week-N rankings and only finds week N-1 should say so.

### LLM close-call node (deliberately deferred)
- [ ] Only where numbers tie: flex decisions inside the margin, trade message phrasing,
      manager notices. Every player name it emits is validated against the roster first.

### Notifications
- [ ] Discord notifier: post the review payload, approve/reject by reaction or reply.
- [ ] Until then, `--watch` in a terminal is the game-day channel.

### Automation
- [ ] `ffagent due`: print which workflows are due now from the slate calendar and league settings.
- [ ] `ffagent tick` on cron/launchd: run what's due, idempotent via the run ledger.
- [ ] Decide where it lives: laptop (misses Sunday mornings when asleep), small VPS, or
      scheduled GitHub Actions with the SQLite file persisted.

### Credentials
- [ ] Move ESPN cookies and LLM keys out of `.env` into the macOS Keychain or 1Password CLI,
      with `.env` kept as a fallback for other platforms.
- [ ] Detect ESPN cookie expiry early (a nightly auth check) rather than at the moment of need.

### Open-source readiness
- [ ] Pick a name (`ffagent` is a placeholder) and a license.
- [ ] CI: offline tests on every push; live contract tests nightly in season on the maintainer's leagues only.
- [ ] README walkthrough for a stranger's league, `ffagent.example.yaml`, contributing notes.
- [ ] Make sure no fixture ever carries personal data (recorder already scrubs; add a CI check).

## Known gaps and debt

- ESPN `transactions()` returns an empty list.
- ESPN `lineups()` is current-week only (past weeks need the matchup view with a scoring period).
- Sleeper projections endpoint is undocumented; the live tests are the early warning.
- ESPN `DAY_TO_DAY` maps to Questionable, which may be noisier than it should be.
- ESPN leagues that allow Out players on IR are not detected; IR-designated only is assumed.
- `ffagent leagues` prints ESPN rosters in roster order rather than slot order.
- The "no replacement on the bench" case only warns; proposing a free-agent pickup belongs to slice 3.
- A rejected proposal is remembered for the week by (kind, slot, in, out); a changed projection does
  not reopen it, which is probably right but untested in practice.
