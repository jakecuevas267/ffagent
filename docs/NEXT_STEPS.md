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
| ESPN provider | Done |
| NFL schedule: kickoffs, byes, locks, slates | Done |
| Projections | Sleeper feed for Sleeper leagues; ESPN's own for ESPN leagues |
| Slice 1 — lineup (Thursday lock) | Done, used live on all 6 leagues |
| Slice 2 — injury start/sit + IR housekeeping | Done, committed; first real game-day use Sun 10/11 |
| Slice 3 — waivers (Tuesday) | Built; first live use Tue 10/13 |
| Slice 4 — trade targets | Not started |
| Slice 5 — league-wide lineup audit | Not started |
| Expert source | FantasyAPISource (premium) through the projection gateway; Upper Hand still pending access |
| Review surface | CLI only |
| Scheduling | Manual (`ffagent run …`, `--watch` for game days) |
| Credentials | `.env` |

Tests: 192 offline, 4 opt-in live. Slices 1 and 2 are committed; slice 3 groundwork is not yet.

## Immediate

- [x] Commit slice 2 (injury workflow, IR housekeeping, Sleeper IR-slot fix). `ad56c3e`
- [ ] Sunday 10/11: first real `ffagent run injury --watch`. Capture what was noisy, what was missed,
      and whether "Doubtful = bench" and "Questionable = watch" are the right defaults.
- [ ] Wednesday 10/14: first real `ffagent run lineup` review for week 6 (not a dry run), to see
      the approve/reject flow and the checklist on the phone.

## Slice 3 — Waivers (Tuesday)

What it should do: for each league, rank available players against my needs (byes, injuries,
positional weakness), pick a drop for each add, size the claim (FAB amount, or claim order for
priority leagues), and chain fallbacks so a lost claim falls through to the next.

Needs before it can be built:
- [x] ESPN transactions view (`mTransactions2`): waiver claims incl. pending with bids, free-agent adds. Done 10/9.
- [x] Sleeper trending adds/drops wired into the provider. Done 10/9.
- [ ] Each league's waiver processing time: Sleeper `waiver_day_of_week` + `daily_waivers` + `daily_waivers_days`
      (a bitmask; encoding still to decode), ESPN `waiverProcessDays` + `waiverProcessHour`. Not needed while runs are manual.
- [x] Rest-of-season value: expert season projections / 17 as points per game; platform next-week projection as fallback.

Defaults in place (tune after a real Tuesday): FAAB 15/7/3% of remaining budget by gain tier, capped one above the richest rival; priority leagues claim only on clear upgrades (≥ 4 ppg), marginal adds listed as post-waiver pickups.
- [ ] Waiver processing times per league, so the run can say "claims process Wed 03:00".
- [ ] ESPN `droppable` flag (undroppable list) is not yet carried into the drop logic.

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
- [x] FantasyAPISource as the first `ExpertSource` behind a gateway with per-player fallback (10/9).
- [ ] Use FantasyAPISource ECR tiers for close calls and its injuries feed (practice reports, probability of playing) in the injury run.
- [ ] Nickname/alias table for the few unmatched rows (e.g. Hollywood/Marquise Brown, Bam/Zonovan Knight).
- [ ] Fallback if Upper Hand stalls: a manual CSV source (`rank,name,pos,team`) dropped in `data/rankings/`.
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
- [x] Name: `ffagent`. License: MIT. Public at github.com/jakecuevas267/ffagent (10/9).
- [ ] CI: offline tests on every push; live contract tests nightly in season on the maintainer's leagues only.
- [ ] README walkthrough for a stranger's league, `ffagent.example.yaml`, contributing notes.
- [ ] Make sure no fixture ever carries personal data (recorder already scrubs; add a CI check).

## Known gaps and debt

- ESPN `lineups()` is current-week only (past weeks need the matchup view with a scoring period).
- Sleeper projections endpoint is undocumented; the live tests are the early warning.
- ESPN `DAY_TO_DAY` maps to Questionable, which may be noisier than it should be.
- ESPN leagues that allow Out players on IR are not detected; IR-designated only is assumed.
- `ffagent leagues` prints ESPN rosters in roster order rather than slot order.
- The "no replacement on the bench" case only warns; proposing a free-agent pickup belongs to slice 3.
- A rejected proposal is remembered for the week by (kind, slot, in, out); a changed projection does
  not reopen it, which is probably right but untested in practice.
