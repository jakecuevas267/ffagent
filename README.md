# ffagent

A human-in-the-loop fantasy football agent for Sleeper and ESPN leagues. It reads your leagues,
combines them with an expert rankings source, proposes waiver claims, lineups, injury swaps, trade
targets and heads-ups for league-mates, and stops for your approval on every one. It never writes to
a league; you make the moves.

Design: [docs/SPEC.md](docs/SPEC.md).

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp ffagent.example.yaml ffagent.yaml   # add your leagues
cp .env.example .env                   # ESPN cookies, LLM key
```

## Use

```bash
.venv/bin/ffagent leagues          # normalized view of every configured league
.venv/bin/ffagent leagues --all    # every roster, not just yours
.venv/bin/ffagent run lineup --dry-run           # what it would propose, no questions asked
.venv/bin/ffagent run lineup                     # review each proposal, get a checklist
.venv/bin/ffagent run lineup --league "Name" --again   # redo one league this week
.venv/bin/ffagent run injury                     # game day: swaps for Out/Doubtful starters, status changes
.venv/bin/ffagent run injury --watch --interval 15   # keep checking until the day's last kickoff
```

Reviews are remembered per league and week in `data/ffagent.sqlite`, so re-running shows the
checklist you already approved instead of asking again.

## Tests

```bash
.venv/bin/pytest                   # fast, offline
.venv/bin/pytest -m live           # opt-in: real Sleeper API, needs FFAGENT_LIVE_SLEEPER_USER
```
