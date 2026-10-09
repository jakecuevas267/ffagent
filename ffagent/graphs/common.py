"""Shared review graph: analyze → human_review (interrupt) → checklist → record."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ffagent.domain.models import Decision


class ReviewState(TypedDict, total=False):
    league: dict
    week: int
    now: str
    summary: dict
    proposals: list[dict]
    decisions: list[dict]
    checklist: list[str]
    error: str


def human_review(state: ReviewState) -> dict[str, Any]:
    if not state.get("proposals"):
        return {"decisions": []}
    raw = interrupt({"league": state["league"], "week": state["week"],
                     "summary": state["summary"], "proposals": state["proposals"]})
    decisions = [Decision.model_validate(d).model_dump(mode="json") for d in raw]
    missing = {p["id"] for p in state["proposals"]} - {d["action_id"] for d in decisions}
    if missing:
        raise ValueError(f"no decision for {sorted(missing)}")
    return {"decisions": decisions}


def checklist(state: ReviewState) -> dict[str, Any]:
    verdicts = {d["action_id"]: d for d in state.get("decisions", [])}
    steps = []
    for p in state.get("proposals", []):
        d = verdicts.get(p["id"])
        if d and d["verdict"] in ("approved", "edited"):
            pl = p["payload"] | (d.get("edited_payload") or {})
            steps.append(pl.get("step") or p["rationale"])
    return {"checklist": steps}


def build_review_graph(analyze: Callable[[ReviewState], dict], checkpointer: BaseCheckpointSaver | None = None,
                       record: Callable[[ReviewState], dict] | None = None):
    g = StateGraph(ReviewState)
    g.add_node("analyze", analyze)
    g.add_node("human_review", human_review)
    g.add_node("checklist", checklist)
    g.add_edge(START, "analyze")
    g.add_conditional_edges("analyze", lambda s: END if s.get("error") else "human_review",
                            {END: END, "human_review": "human_review"})
    g.add_edge("human_review", "checklist")
    if record:
        g.add_node("record", record)
        g.add_edge("checklist", "record")
        g.add_edge("record", END)
    else:
        g.add_edge("checklist", END)
    return g.compile(checkpointer=checkpointer)
