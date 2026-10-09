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
- [x] ESPN `droppable` flag honoured in the drop logic.
- [x] Sleeper trending adds/drops drive speculative fliers and "being dropped" flags in every league.

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

## After the five slices, in this order

### Phase 0 — Credentials out of `.env`
Today five values live in `.env`: ESPN `espn_s2` and `SWID`, the LLM key, and the expert API key
and URL. Replace the file with a `SecretsProvider` that the app asks by name, with three backends:

- **env / `.env`** — stays as the zero-setup default and the CI/test path.
- **OS keyring** (`keyring` library: macOS Keychain, Windows Credential Locker, Linux Secret Service)
  — free, local, right for a laptop; not reachable from inside a container.
- **AWS SSM Parameter Store, SecureString, standard tier** — the chosen remote store. Free: standard
  parameters have no charge, the AWS-managed `aws/ssm` KMS key has no key fee, and KMS request
  charges at a few dozen reads a week are negligible. Works from a laptop, a container or a cloud
  scheduler alike. (Secrets Manager is the wrong pick at $0.40 per secret per month.)
  Bootstrap: one AWS credential (an IAM user or role allowed only `ssm:GetParameter` on the
  `/ffagent/*` path and `kms:Decrypt` on `aws/ssm`) replaces five secrets on disk, and that one can
  live in the keyring or an instance role.

Alternatives looked at and why not: GCP Secret Manager's free tier is 6 active versions, which we
would exceed on the first rotation; Doppler (free, 3 users) and Infisical (free, 5 identities) are
good products but another account for a solo project; HashiCorp Vault is overkill to self-host.

- [ ] `ffagent/secrets.py`: `get(name)` resolving in order ssm → keyring → env, backend chosen by
      `FFAGENT_SECRETS=ssm|keyring|env`; everything that reads `os.environ` for a secret goes through it.
- [ ] `ffagent secrets set <name>` / `secrets check` commands, so cookies can be refreshed without
      editing files; `check` reports which backend answered and which names are missing.
- [ ] Early expiry detection for the ESPN cookies (one cheap authenticated call) surfaced by `check`.
- [ ] Never log or print values; redact in errors (already the case for ESPN).
- [ ] Docs: the AWS setup in five CLI commands, and the IAM policy JSON.

### Phase A — Docker instead of venv
Containerize the CLI so setup is `docker compose run ffagent …` on any machine, and so the UI and
scheduler later run in the same image.
- [ ] `Dockerfile` (slim Python base, non-root, the package installed), `compose.yaml` with `data/`
      as a volume and `.env` passed through; `ffagent.yaml` mounted read-only.
- [ ] Image build and the offline test suite in CI.
- [ ] README setup becomes two commands; venv stays documented as the dev path.

### Phase B — UI to trigger everything
A local web UI (served from the container) that replaces the terminal as the review surface:
buttons for each workflow per league, the proposal list with approve/reject/edit and the checklist,
run history and what was decided, status lines (sources, pending claims, verify results), and the
game-day watch as a live panel.
- [ ] Thin HTTP layer over the same graphs (the CLI and UI share one code path; the interrupt/resume
      contract is the API).
- [ ] Review page per run; decisions persist through the same store.
- [ ] Settings page for `ffagent.yaml` values that are safe to edit (leads, thresholds, max claims).
- [ ] Discord notifier can then just deep-link into the UI.

### Phase C — Evals (last, and a long one)
Comprehensive evaluation of every workflow, deterministic and LLM-judged, with frozen weekly
snapshots as the datasets. This is the step we spend real time on; nothing ships after it without
passing it.
- [ ] **Snapshot recorder:** each real run stores its full inputs (rosters, statuses, projections,
      schedule, free agents, settings) so any past week can be replayed exactly.
- [ ] **Deterministic evaluators** (pure, run on every PR):
      lineup optimality against brute force on small rosters; no invalid players, slots or locks in any
      proposal; bids never exceed budget or caps; IR rules per league honoured; waiver drops never
      empty a slot; idempotence (same inputs, same proposals); rejection memory holds; verify logic
      against recorded transactions.
- [ ] **Hindsight evaluators** (replay against what actually happened):
      lineup proposals vs actual points scored; injury swaps vs who actually played; waiver pickups vs
      their production over the following weeks; trade targets vs subsequent value. Reported as
      distributions over the season, not pass/fail.
- [ ] **LLM-as-judge** (opt-in, real model, rubric-scored):
      rationale cites the evidence shown; no player, team or number appears that is not in the inputs;
      checklist steps are unambiguous on a phone; close calls are explained as close; manager notices
      are polite and specific; consistency across leagues for the same situation.
- [ ] **Harness:** one command runs everything, writes a report per workflow with trends over
      snapshots, and gates merges on the deterministic set.
- [ ] **Expert-source evals:** coverage and match rate of the external source against platform rosters
      per week, alias misses, and value-scale sanity (expert vs platform agreement).

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

### Automation (after Phase B; the UI's run buttons become the scheduler's hooks)
- [ ] `ffagent due`: print which workflows are due now from the slate calendar and league settings.
- [ ] `ffagent tick` on cron/launchd: run what's due, idempotent via the run ledger.
- [ ] Decide where it lives: laptop (misses Sunday mornings when asleep), small VPS, or
      scheduled GitHub Actions with the SQLite file persisted.

### Credentials
See Phase 0 above.

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
