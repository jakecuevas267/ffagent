"""The advise node: the manager's playbook applied by an LLM to a candidate set the deterministic
layer produced. It can only choose from those candidates, must name the principle it applied, and
is validated before anything reaches review. Provider-agnostic through init_chat_model."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Advice(BaseModel):
    add_id: str = Field(description="player id of the pickup, from the candidates list")
    drop_id: str | None = Field(default=None, description="player id to drop, from the droppable list")
    action: str = Field(description="claim | pickup | stash | skip")
    bid: int | None = Field(default=None, description="FAAB bid when the league uses FAAB")
    principle: str = Field(description="the playbook principle applied, quoted or closely paraphrased")
    rationale: str = Field(description="one or two sentences, citing the facts shown")


class AdviceOut(BaseModel):
    proposals: list[Advice] = Field(default_factory=list, description="ordered by preference; empty if nothing is worth doing")
    watch: list[str] = Field(default_factory=list, description="players to keep an eye on, with a reason")
    holds_questioned: list[str] = Field(default_factory=list, description="held players the playbook would actually let go, with a reason")


@dataclass
class AdviseResult:
    proposals: list[Advice]
    watch: list[str] = field(default_factory=list)
    holds_questioned: list[str] = field(default_factory=list)
    model: str = ""
    error: str | None = None
    rejected: list[str] = field(default_factory=list)   # advice dropped by validation, with why


SYSTEM = """You are a fantasy football manager's assistant. You apply the manager's own playbook to a
candidate set that a deterministic layer has already prepared. Rules:
- Choose only among the listed pickup candidates and droppable players; never invent players.
- Held players are not droppable. If you believe the playbook would let one go, say so in
  holds_questioned instead of proposing it.
- Every proposal names the playbook principle it applies. If no principle supports an action, skip it.
- Prefer fewer, better actions. An empty proposals list is a valid answer.
- Respect the league format facts (dynasty vs redraft, superflex, FAAB vs priority, roster limits).
- Bids must not exceed the remaining budget. Use priority only for clear upgrades.
Answer only with the structured output."""


def load_playbook(playbook_dir: Path | str, name: str) -> str:
    d = Path(playbook_dir)
    for candidate in (d / f"{name}.md", d / f"{name}.example.md"):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return ""


def build_messages(playbook: str, league: dict, roster: list[dict], candidates: list[dict], droppable: list[dict],
                   recent_decisions: list[dict], budget: int | None, max_actions: int) -> list[tuple[str, str]]:
    user = {
        "league_format": league, "faab_remaining": budget, "max_actions": max_actions,
        "my_roster": roster, "pickup_candidates": candidates, "droppable_players": droppable,
        "my_recent_decisions": recent_decisions,
    }
    return [("system", SYSTEM + "\n\n# Manager's playbook\n" + (playbook or "(no playbook written yet)")),
            ("human", "Decide this week's waiver actions.\n\n" + json.dumps(user, indent=1, default=str))]


def validate(out: AdviceOut, candidate_ids: set[str], droppable_ids: set[str], budget: int | None,
             max_actions: int) -> tuple[list[Advice], list[str]]:
    ok, rejected, seen = [], [], set()
    for a in out.proposals:
        if a.action == "skip":
            continue
        if a.add_id not in candidate_ids:
            rejected.append(f"{a.add_id}: not a candidate"); continue
        if a.drop_id is None or a.drop_id not in droppable_ids:
            rejected.append(f"{a.add_id}: drop {a.drop_id!r} is not droppable"); continue
        if a.add_id in seen:
            continue
        if budget is not None:
            if a.bid is None or a.bid < 1:
                a.bid = 1
            a.bid = min(a.bid, budget)
        else:
            a.bid = None
        if a.action not in ("claim", "pickup", "stash"):
            a.action = "pickup"
        seen.add(a.add_id)
        ok.append(a)
        if len(ok) >= max_actions:
            break
    return ok, rejected


class Adviser:
    def __init__(self, llm=None, model: str = "", playbook: str = ""):
        self._llm = llm            # object with .invoke(messages) -> AdviceOut; None = disabled
        self.model = model
        self.playbook = playbook

    @classmethod
    def from_config(cls, model: str, playbook: str, temperature: float | None = None):
        from langchain.chat_models import init_chat_model
        kwargs = {} if temperature is None else {"temperature": temperature}  # some models allow only the default
        llm = init_chat_model(model, **kwargs).with_structured_output(AdviceOut, method="json_schema")
        return cls(llm, model, playbook)

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    def advise(self, league: dict, roster: list[dict], candidates: list[dict], droppable: list[dict],
               recent_decisions: list[dict], budget: int | None, max_actions: int) -> AdviseResult:
        if not self.enabled:
            return AdviseResult([], model="", error="advise node off")
        candidate_ids = {c["id"] for c in candidates}
        droppable_ids = {d["id"] for d in droppable}
        try:
            out = self._llm.invoke(build_messages(self.playbook, league, roster, candidates, droppable,
                                                 recent_decisions, budget, max_actions))
            if not isinstance(out, AdviceOut):
                out = AdviceOut.model_validate(out)
        except Exception as e:  # noqa: BLE001 - the deterministic proposals stand if the model fails
            log.warning("advise node failed: %s", e)
            return AdviseResult([], model=self.model, error=f"{type(e).__name__}: {e}"[:160])
        ok, rejected = validate(out, candidate_ids, droppable_ids, budget, max_actions)
        return AdviseResult(ok, out.watch, out.holds_questioned, self.model, None, rejected)


# --- Trades -------------------------------------------------------------------------------------

class TradeAdvice(BaseModel):
    offer_id: str = Field(description="id of the offer, from the candidate offers list")
    action: str = Field(description="propose | skip")
    message: str = Field(description="the message to send the other manager: short, friendly, specific")
    principle: str = Field(description="the playbook principle applied")
    rationale: str = Field(description="why this offer, citing the facts shown")


class TradeAdviceOut(BaseModel):
    proposals: list[TradeAdvice] = Field(default_factory=list)
    watch: list[str] = Field(default_factory=list, description="players or partners worth watching for a future deal")


TRADE_SYSTEM = """You are a fantasy football manager's assistant. You apply the manager's own playbook to trade
offers a deterministic layer has already prepared (each helps my lineup and does not hurt the
partner's by much, within a fairness band). Rules:
- Choose only among the listed offers; never invent players or offers.
- Every proposal names the playbook principle it applies and comes with the message to send.
- Prefer offers the partner is likely to accept; say why they would.
- Respect the league format facts (dynasty values future, superflex values QBs, deadline).
- An empty proposals list is a valid answer.
Answer only with the structured output."""


def validate_trades(out: TradeAdviceOut, offer_ids: set[str], max_actions: int) -> tuple[list[TradeAdvice], list[str]]:
    ok, rejected, seen = [], [], set()
    for a in out.proposals:
        if a.action == "skip":
            continue
        if a.offer_id not in offer_ids:
            rejected.append(f"{a.offer_id}: not an offer on the list"); continue
        if a.offer_id in seen:
            continue
        seen.add(a.offer_id); ok.append(a)
        if len(ok) >= max_actions:
            break
    return ok, rejected


@dataclass
class TradeAdviseResult:
    proposals: list[TradeAdvice]
    watch: list[str] = field(default_factory=list)
    model: str = ""
    error: str | None = None
    rejected: list[str] = field(default_factory=list)


class TradeAdviser:
    def __init__(self, llm=None, model: str = "", playbook: str = ""):
        self._llm, self.model, self.playbook = llm, model, playbook

    @classmethod
    def from_config(cls, model: str, playbook: str, temperature: float | None = None):
        from langchain.chat_models import init_chat_model
        kwargs = {} if temperature is None else {"temperature": temperature}
        return cls(init_chat_model(model, **kwargs).with_structured_output(TradeAdviceOut, method="json_schema"), model, playbook)

    @property
    def enabled(self) -> bool:
        return self._llm is not None

    def advise(self, league: dict, my_roster: list[dict], offers: list[dict], recent_decisions: list[dict],
               max_actions: int) -> TradeAdviseResult:
        if not self.enabled:
            return TradeAdviseResult([], error="advise node off")
        user = {"league_format": league, "max_actions": max_actions, "my_roster": my_roster,
                "candidate_offers": offers, "my_recent_decisions": recent_decisions}
        msgs = [("system", TRADE_SYSTEM + "\n\n# Manager's playbook\n" + (self.playbook or "(no playbook written yet)")),
                ("human", "Pick the trade offers worth sending this week and write the messages.\n\n" + json.dumps(user, indent=1, default=str))]
        try:
            out = self._llm.invoke(msgs)
            if not isinstance(out, TradeAdviceOut):
                out = TradeAdviceOut.model_validate(out)
        except Exception as e:  # noqa: BLE001
            log.warning("trade advise failed: %s", e)
            return TradeAdviseResult([], model=self.model, error=f"{type(e).__name__}: {e}"[:160])
        ok, rejected = validate_trades(out, {o["id"] for o in offers}, max_actions)
        return TradeAdviseResult(ok, out.watch, self.model, None, rejected)
