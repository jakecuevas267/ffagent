"""Interactive review in the terminal: one prompt per proposal, then a checklist."""
from __future__ import annotations

import sys
from collections.abc import Callable

from ffagent.domain.models import Decision

Prompt = Callable[[str], str]


def print_review(payload: dict, out=None) -> None:
    out = out or sys.stdout
    s = payload["summary"]
    lg = payload["league"]
    print(f"\n== {lg.get('name') or lg['league_id']} — week {payload['week']} — {s['team']}", file=out)
    print(f"   projected {s['projected_before']} → {s['projected_after']}  (first kickoff {s['first_kickoff']})", file=out)
    for w in s.get("warnings", []):
        print(f"   ! {w}", file=out)
    for f in s.get("flags", []):
        print(f"   ? {f}", file=out)
    for c in s.get("close_calls", []):
        print(f"   ~ close call: {c}", file=out)
    for n, p in enumerate(payload["proposals"], 1):
        print(f"\n   [{n}] {p['rationale']}", file=out)
        for e in p["evidence"]:
            print(f"       - {e}", file=out)
        if p["payload"].get("locks_at"):
            print(f"       locks at {p['payload']['locks_at']}", file=out)


def collect_decisions(proposals: list[dict], prompt: Prompt | None = None, auto_approve: bool = False) -> list[dict]:
    prompt = prompt or input  # resolved at call time so tests can patch builtins.input
    decisions = []
    for n, p in enumerate(proposals, 1):
        if auto_approve:
            decisions.append(Decision(action_id=p["id"], verdict="approved").model_dump(mode="json"))
            continue
        while True:
            ans = prompt(f"   [{n}] {p['rationale']} — (a)pprove / (r)eject / (n)ote+reject: ").strip().lower()
            if ans in ("a", "approve", "y", "yes"):
                decisions.append(Decision(action_id=p["id"], verdict="approved").model_dump(mode="json"))
                break
            if ans in ("r", "reject", "n", "no"):
                note = prompt("       why? (optional): ").strip() if ans in ("n", "no") else ""
                decisions.append(Decision(action_id=p["id"], verdict="rejected", note=note).model_dump(mode="json"))
                break
            print("       please answer a or r")
    return decisions


def print_checklist(league_name: str, steps: list[str], out=None) -> None:
    out = out or sys.stdout
    if not steps:
        print(f"\n   {league_name}: nothing approved, no changes to make.", file=out)
        return
    print(f"\n   To do in the app for {league_name}:", file=out)
    for s in steps:
        print(f"     [ ] {s}", file=out)
