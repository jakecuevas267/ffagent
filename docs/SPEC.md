# ffagent — Fantasy Football Weekly Agent

Spec and test-driven design. Status: draft v0.2 (2026-10-08). `ffagent` is a placeholder name.

## 1. Purpose

A LangGraph agent that does the weekly fantasy football chores across any number of Sleeper and ESPN leagues: waiver targets, lineup setting, game-day injury checks, trade targets, and a league-wide audit that catches other managers starting injured or bye-week players so you can give them a heads-up. It reads league data, combines it with a trusted expert source, proposes specific actions, and stops for human approval on every one.

The agent **never writes to a league**. Neither platform has a supported write API, so an approved action becomes a checklist item the human executes in the app. The agent then verifies on its next read that the move happened.

### Goals

- One normalized view of every league regardless of platform.
- Runs triggered by the real NFL schedule and each league's own settings, not fixed weekdays.
- Every recommendation is traceable to data: rank, projection, injury status, roster need.
- Open source: leagues, credentials, expert sources, LLM provider and notification channel are all configuration or plugins.

### Non-goals (v1)

- Automated roster moves, waiver claims or trade offers.
- Draft assistance, DFS, best ball, betting.
- Its own projection model. It consumes rankings; it does not produce them.
- A web UI.

## 2. Data sources

| Source | Access | Auth | Notes |
|---|---|---|---|
| Sleeper | Official REST API, read-only | None | 1,000 calls/min. Player dump (~5MB) at most once a day. No free-agent endpoint: free agents = all players minus rostered. |
| ESPN | Unofficial v3 endpoints, called directly with httpx (`lm-api-reads.fantasy.espn.com`), views `mSettings/mTeam/mRoster/mMatchup/mStatus` and `kona_player_info` for the player pool | `espn_s2` + `SWID` cookies for private leagues | Can break without notice. Cookies expire; a 401 raises `AuthExpired` with the fix. Read-only in this project. ESPN player ids are kept as `espn:<id>`; the Sleeper crosswalk is used only when a source keyed by Sleeper ids is needed. |
| NFL schedule | Sleeper `/state/nfl` for the current week; kickoff times, game status and teams on bye from ESPN's public scoreboard feed | None | Unofficial but verified 2026-10-08. Sleeper's player `bye_week` field is empty, so byes come from here. |
| Expert source | Plugin. First implementation: Upper Hand Fantasy (paid, login-gated SPA, no public API) | User's own session | Ingestion method is an open spike (section 10). |

### Projection sources for the proof of concept

Until Upper Hand access is settled, rankings come from platform projections, which are good enough to prove the workflows:

| Source | Access | Keyed by | Notes |
|---|---|---|---|
| Sleeper projections (`api.sleeper.app/projections/nfl/{season}/{week}`) | Undocumented but public, no auth | Sleeper id | Weekly `pts_ppr` / `pts_half_ppr` / `pts_std` plus raw stat lines, so points can be recomputed under any league's scoring. Default source for Sleeper leagues. Verified 2026-10-08. |
| ESPN projections (`kona_player_info` view) | Unofficial, no auth needed for the public default league | ESPN id | Weekly projected points under ESPN's default scoring sets. Default source for ESPN leagues. Verified 2026-10-08. |
| FantasyPros consensus rankings | Official REST API, free key on request | Name/team | Expert consensus with tiers; optional plugin once a key exists. |
| Upper Hand Fantasy | Pending sanctioned access | Name | The manager's trusted source; slots in as another `ExpertSource` without changing the workflows. |

Scoring settings are read from each league automatically, so projections built from raw stat lines are scored per league rather than assuming PPR.

### Expert source rules

- Paid content is never committed, logged at info level, or included in test fixtures. Fixtures for expert sources are synthetic.
- Each `RankingSet` carries `source`, `kind`, `format`, `week` and `as_of`. A workflow that needs week-N rankings and finds only stale ones interrupts and asks whether to wait or proceed without.
- The agent works with zero expert sources configured, falling back to platform projections, and says so in its rationale.

## 3. Domain model

Pydantic models in `ffagent/domain`. Providers map into these; nothing downstream sees platform-specific shapes.

- `LeagueRef` — platform, league id, season, my team id, display name.
- `LeagueSettings` — roster slots, scoring rules, format (redraft/keeper/dynasty), waiver type (FAAB, rolling, reverse standings), waiver processing schedule, trade deadline, lineup lock mode.
- `Player` — canonical id, name, position(s), NFL team, bye week, injury status, practice status, platform ids.
- `Team` — owner, roster entries (player, slot, starter/bench/IR), record, FAAB remaining or waiver priority.
- `Matchup` — week, my team, opponent, projected and actual points.
- `Transaction` — adds, drops, trades, with timestamps.
- `RankingSet` — ordered entries of (player, rank, tier, optional value, optional FAB recommendation).
- `Slate` — a calendar date with one or more NFL games, their kickoff times and teams.
- `ProposedAction` — kind (`add_drop`, `waiver_claim`, `lineup_swap`, `trade_offer`, `manager_notice`), league, payload, rationale, evidence list, confidence.
- `Decision` — action id, verdict (`approved`, `edited`, `rejected`), edited payload, note, timestamp.

### Player identity

The canonical id is the Sleeper player id; the Sleeper player dump carries cross-references to ESPN ids. Expert-source names resolve by normalized name + position + team. A name that does not resolve exactly goes on an `unresolved` list shown to the human. The agent never guesses a match.

## 4. Architecture

Python, LangGraph. Python is the right choice here: `espn-api` is the most maintained ESPN client and is Python-only.

```
ffagent/
  domain/       models, player identity
  providers/    LeagueProvider protocol; sleeper.py, espn.py
  sources/      ExpertSource protocol; upperhand.py, manual.py
  schedule/     slate calendar, trigger computation
  analysis/     pure functions: lineup, waivers, injuries, trades
  graphs/       state, shared nodes, one graph per workflow
  review/       CLI review surface, notifier plugins
  store/        SQLite: checkpoints, cache, decisions, run ledger
  cli.py
tests/
  unit/  graph/  contract/  fixtures/
```

### Protocols

```python
class LeagueProvider(Protocol):
    def settings(self, ref: LeagueRef) -> LeagueSettings: ...
    def teams(self, ref: LeagueRef) -> list[Team]: ...
    def matchup(self, ref: LeagueRef, week: int) -> Matchup: ...
    def free_agents(self, ref: LeagueRef) -> list[Player]: ...
    def transactions(self, ref: LeagueRef, week: int) -> list[Transaction]: ...
    def lineups(self, ref: LeagueRef, week: int) -> dict[TeamId, list[RosterEntry]]: ...  # every team's starters

class ExpertSource(Protocol):
    name: str
    def rankings(self, kind: RankingKind, fmt: ScoringFormat, week: int | None) -> RankingSet | None: ...
```

### Layering rule

Arithmetic lives in `analysis/` as pure, deterministic functions. The LLM is used only to (a) break ties and weigh context the numbers miss, (b) write rationales, and (c) draft trade messages. Every player an LLM node names is validated against the league's actual roster and free-agent sets; an unknown player fails the node rather than reaching the human.

The LLM is created through `init_chat_model`, so the provider is configuration. Default: Claude.

### Shared graph shape

Each workflow is its own `StateGraph`, built from shared nodes:

```
resolve_leagues → load_league_context (fan-out, one branch per league)
               → load_expert_rankings → [freshness gate]
               → analyze (deterministic) → propose (LLM + validation)
               → human_review (interrupt) → build_checklist → record
```

- Fan-out uses `Send`, so one failing league (for example expired ESPN cookies) is reported and skipped without blocking the others.
- Thread id is `{workflow}:{league}:{season}:{week}:{slate?}`. Re-running a trigger resumes or no-ops; it never duplicates.
- Checkpointer: `SqliteSaver`. Interrupted runs survive restarts and can wait days.

### Human-in-the-loop contract

`human_review` calls `interrupt()` with a list of `ProposedAction`s. It resumes with one `Decision` per action. Rules:

- Nothing is ever marked actionable without an `approved` or `edited` decision.
- Editing changes the payload (swap the drop candidate, change the FAB bid) and re-runs validation before the checklist is built.
- Rejecting with a note is stored and fed to later runs in the same week, so the agent does not re-propose the same thing.
- A run with nothing to propose does not interrupt. It sends a one-line "no changes" notice.
- Pending reviews expire at the relevant deadline (waiver processing, kickoff) and are recorded as `expired`.

Because the agent cannot write, `build_checklist` emits manual steps with deep links into the app. A `verify` step at the start of the next run for that league compares approved actions against the league's transactions and lineup and flags any that were not carried out.

## 5. Scheduling

**v1 is triggered manually.** `ffagent run <workflow> [--league]` runs a workflow now, and `ffagent due` prints which workflows the schedule logic thinks are due so the human can run them. The run ledger still records every run, so re-running is idempotent.

The schedule logic itself is built in v1 because it is needed to scope work (which slate, which week, which leagues have waivers pending). Wiring it to a scheduler is a post-v1 step: `ffagent tick`, called on an interval by cron, launchd or GitHub Actions, will compute due runs from the slate calendar and league settings, consult the run ledger, and start only what has not run.

The agent has no fixed weekdays:

| Workflow | Trigger | Default |
|---|---|---|
| Waivers | Lead time before each league's next waiver processing | 18h before; usually lands Tuesday |
| Lineup | Lead time before the first kickoff of the NFL week | 24h before; usually lands Wednesday night for a Thursday game |
| Injury watch | For every slate in the week: morning of, and before each kickoff window once inactives are published | 9am local, and 75 min before kickoff |
| Trade targets | After the week's waivers process, and on demand | Usually Wednesday |
| League audit | Same cadence as injury watch, plus once after the week's first lineup run for byes | — |

"Every slate" covers Thursday, Sunday and Monday, and equally Saturday games late in the season, holiday games and international early kickoffs, with no special cases. All lead times are per-league configurable.

## 6. Workflows

### 6.1 Waiver targets

Inputs: free agents, my roster, FAAB remaining or waiver priority, league-mates' rosters and budgets, expert waiver rankings with FAB recommendations, trending adds, upcoming byes and injuries on my roster.

Deterministic: rank available players by expert rank adjusted for my positional need; pick a drop candidate per add (lowest rest-of-season value that is not a required starter); scale the recommended FAB percentage to my remaining budget and cap it by what competing teams can bid.

Output per league: ordered claims, each with add, drop, bid or priority order, and rationale. Fallback claims are ordered so a lost bid falls through to the next.

### 6.2 Lineup

Deterministic: fill roster slots to maximize expert weekly rank (fallback: platform projection), honoring slot eligibility, byes and injury status. Flex decisions within a small rank margin are marked as close calls and passed to the LLM with matchup context.

Output: full starting lineup as a diff against the current one. Players in the week's earliest games are highlighted because they lock first. If the lineup is already optimal, no interrupt.

### 6.3 Injury watch

Runs per slate. Compares each league's current lineup with fresh injury, practice and inactive data for players on that slate's teams.

Proposes a swap when a starter is out, doubtful, inactive, or downgraded since the last approved lineup, with the best eligible replacement whose game has not started. If the bench has no viable replacement, proposes a free-agent add where the league allows same-day pickups. Also flags starters in later slates who are trending the wrong way, so the human can hold a bench option from a later game.

### 6.4 Trade targets

Deterministic: score every team's strength per position from starters' rest-of-season values against the league median, with injured players discounted by expected absence. Identify my surplus and need positions and each other team's. A candidate partner is a team whose need matches my surplus and whose surplus matches my need. Generate offers that are within a fairness band on trade values and that improve both starting lineups.

Injuries drive this directly: an injury on my roster creates a need; an injury on another roster creates a partner.

Output: up to N ranked offers per league, each with give, get, the lineup effect for both sides, and a drafted message to the other manager. The human sends the offer. Skipped after the league's trade deadline.

### 6.5 League audit (other managers' lineups)

Runs over every team in every league, not just mine. For each team's current starting lineup, flags:

- a starter on bye this week;
- a starter who is out, on IR, suspended, doubtful, or declared inactive;
- an empty starting slot;
- optionally, a starter whose game has already finished with zero snaps (caught late, still useful for a Monday-night swap elsewhere).

Each flag is scoped to what can still be fixed: a player whose game has started is reported but marked unfixable. Trade-target analysis (6.4) reuses the same findings, since a manager starting an injured player is also a manager with a need.

Output: one `manager_notice` per affected team with the issues, the obvious bench fix if one exists, and a short drafted message. The human approves which notices to send and sends them in the league chat or directly; the agent does not message anyone. Teams already flagged earlier in the same week are not re-flagged unless something new appears, so a manager who chooses to leave a player in is not nagged.

Deterministic: all of it. No LLM is needed beyond phrasing the message, and the message template works without one.

## 7. Configuration

```yaml
# ffagent.yaml (committed example uses placeholders)
season: 2026
timezone: America/Denver
llm: { model: "anthropic:claude-fable-5-1" }
leagues:
  - { platform: sleeper, league_id: "…", me: "<username>" }
  - { platform: espn, league_id: 123456, team_id: 4 }
sources:
  - { type: upperhand }
notify:
  - { type: stdout }
schedule:
  waivers_lead_hours: 18
  lineup_lead_hours: 24
  inactives_lead_minutes: 75
```

Secrets (`ESPN_S2`, `ESPN_SWID`, LLM keys, expert-source session) come only from the environment or `.env`, which is git-ignored. Secrets are redacted from logs, errors and recorded fixtures. Sleeper leagues can also be auto-discovered from a username.

## 8. Test plan

Tests are written before the code they cover, in the milestone order of section 9. No test in the default suite touches the network or a real LLM.

### Unit — domain and analysis (pure functions)

- Lineup: fills standard, superflex and two-flex slot layouts; never starts a bye, out or IR player when an alternative exists; respects multi-position eligibility; stable on rank ties; reports "already optimal".
- Waivers: never proposes a rostered player; drop candidate is never a required starter; bids never exceed remaining FAAB; total of all bids respects the budget when fallbacks are chained; handles rolling-priority leagues with no bids.
- Injuries: detects status downgrades between two snapshots; ignores players whose game has started; picks only replacements whose game has not started.
- Trades: need/surplus scoring against known rosters; no offers after the deadline; every offer improves both lineups; fairness band enforced.
- League audit: flags bye, out, IR, doubtful, inactive and empty slots for every team; marks started games as unfixable; suggests a bench fix only when an eligible, unlocked player exists; does not re-flag an issue already reported this week; excludes my own team (covered by injury watch) unless asked.
- Identity: Sleeper-to-ESPN crosswalk; name normalization (suffixes, punctuation, team changes); ambiguous names land on `unresolved`, never auto-matched.
- Schedule: trigger times for a normal week, a Saturday slate, a Wednesday holiday game, an international kickoff, and a bye-heavy week; daylight-saving boundary; a tick run twice starts nothing new.

### Provider — recorded fixtures

- Sleeper and ESPN adapters map recorded responses into domain models; one fixture league per format (redraft FAAB, dynasty, rolling waivers).
- Free-agent derivation for Sleeper equals all players minus rostered.
- Expired ESPN cookies raise a typed `AuthExpired` error with a human-readable fix.
- Recorded fixtures are scrubbed: no cookies, no real usernames, no paid content.

### Graph — LangGraph with in-memory checkpointer and a fake chat model

- Each workflow interrupts with the expected actions and resumes correctly on approve, edit and reject.
- An edited action is re-validated; an invalid edit returns to review rather than reaching the checklist.
- No path reaches `build_checklist` without a decision for every action.
- An LLM response naming a player outside the roster/free-agent set fails validation.
- One failing league in the fan-out does not block the others.
- Stale expert rankings route to the freshness interrupt; no expert source routes to the projection fallback.
- A run interrupted, persisted and reloaded from SQLite resumes at the same point.
- Verify step flags an approved action that does not appear in the next read.

### Contract — opt-in, live

`pytest -m live` hits real Sleeper and ESPN endpoints with the developer's own leagues and asserts response shapes only. Run nightly in season to catch ESPN changes early. Never runs in CI for forks.

### Evaluation — opt-in, real LLM

A small set of frozen week snapshots with rubric checks on rationales (cites evidence, names no invalid players, close calls flagged). Tracked over time, not a merge gate.

## 9. Milestones

Built as vertical slices, in the order the features are used during a week. Each slice is usable end to end (data, analysis, approval, checklist, verify) before the next starts, and each is first proven on Sleeper, then on ESPN.

- **M0 Spikes.** Kickoff feed and ID crosswalk. *Done 2026-10-08.*
- **M1 Foundation.** Domain models, identity, Sleeper provider, config, `ffagent leagues`. *Done 2026-10-08 (offline tests); live check against real leagues pending.*
- **M2 Lineup (Thursday lock).** Slate calendar, projections source, lineup optimizer, first graph with interrupt, CLI review, checklist. *Done 2026-10-08 on Sleeper; verify-on-next-run and ESPN still to come.* First real use: week 6.
- **M3 Injury start/sit (game days).** Status snapshots and diffs, per-slate runs, replacement search, `--watch` loop for a game day.
- **M4 Waivers (Tuesday).** Free-agent ranking, need scoring, drop candidates, FAB sizing, fallback chains.
- **M5 Trade targets.** Need/surplus per team, partner matching, fairness band, drafted messages.
- **M6 League audit.** Every team's lineup checked for byes, injuries and empty slots; manager notices.
- **M7 ESPN provider.** *Done 2026-10-08:* settings, teams, lineups, free agents, matchups and ESPN's own league-scored weekly projections, from recorded (scrubbed) fixtures of two real leagues. Transactions view still TODO. Each later slice is checked on both platforms.
- **Post-v1.** Credentials outside `.env` (keychain / secret manager), scheduler-driven runs, Discord notifier, LLM close-call node where it earns its place.

## 10. Open questions

1. **Upper Hand ingestion.** Preferred: ask the publisher for a sanctioned export or endpoint for subscribers, since this will be open source and should not encourage scraping a paid product. Fallback: a manual-file source the user drops each week's rankings into. Session-based fetching only with explicit permission.
2. **ESPN and Sleeper lock rules.** Per-game locking is assumed as the default; read it from league settings where exposed and confirm in M1/M2.
3. **License** for the open-source release.

## 11. Decisions log

- 2026-10-08: Review surface is a CLI (`ffagent review`) for v1; Discord notifier is the first post-v1 addition.
- 2026-10-08: v1 is run manually; automatic scheduling (`ffagent tick`) is post-v1.
- 2026-10-08: Trade targets run weekly after waivers clear, plus on demand.
- 2026-10-08: Added the league audit workflow (6.5) for notifying other managers.
- 2026-10-08: Build order changed to vertical slices: lineup → injury start/sit → waivers → trade targets → league audit. Credentials stay in `.env` and runs stay manual until all five work.
- 2026-10-08: ESPN provider calls the v3 endpoints directly instead of the `espn-api` library, so fixtures and tests see raw shapes. ESPN leagues use ESPN's own projections (already scored under league rules) rather than the Sleeper feed, avoiding an id crosswalk.
- 2026-10-08: Slice 1 (`ffagent run lineup`) works end to end against live Sleeper leagues. Optimizer is a greedy transversal-matroid fill (optimal, no LLM). IR/taxi players are never lineup candidates.
- 2026-10-08: Rankings for the proof of concept come from Sleeper and ESPN projections (no keys); FantasyPros optional; Upper Hand when available.
- 2026-10-08: Leagues where the manager is commissioner without a roster are supported; only the league audit runs there.
- 2026-10-08: M0 verified: ESPN scoreboard feed returns kickoffs and byes; Sleeper player dump has `espn_id` (4,470 active players) and injury/practice fields.
- 2026-10-08: Sanctioned Upper Hand access has been requested; ingestion decision pending that answer.
