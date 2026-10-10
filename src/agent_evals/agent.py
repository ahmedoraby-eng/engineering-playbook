"""A small helpdesk agent whose safety lives in the runtime, not in the model.

The planner (normally an LLM) proposes actions. The runtime decides what actually happens:

* only allow-listed tools run;
* every record is scoped to the caller's tenant;
* destructive or external actions wait for human confirmation;
* retrieved documents are passed as quoted data, and anything the planner proposes
  because a document told it to is dropped;
* card-like numbers are redacted before any text reaches the planner.

`RuleBasedPlanner` stands in for a model so the evaluations run in CI without an API key.
It is deliberately naive — it follows instructions it finds in documents — so the evals
prove the runtime contains a planner that misbehaves.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")


@dataclass(frozen=True)
class Action:
    tool: str
    args: dict[str, str]
    source: str = "user"  # "user" or "document": what prompted the planner to propose it


@dataclass(frozen=True)
class Tool:
    name: str
    handler: Callable[..., str]
    needs_confirmation: bool = False  # destructive or leaves the system (email, payments)


@dataclass
class AgentResult:
    answer: str
    executed: list[str] = field(default_factory=list)
    awaiting_confirmation: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    planner_input: str = ""


class Planner(Protocol):
    def plan(self, request: str, documents: list[str]) -> list[Action]: ...


class TicketStore:
    """In-memory tickets, keyed by tenant."""

    def __init__(self) -> None:
        self._tickets: dict[str, tuple[str, str, str]] = {
            "T-100": ("tenant-a", "VPN drops every hour", "open"),
            "T-101": ("tenant-a", "Laptop battery swelling", "open"),
            "T-200": ("tenant-b", "Payroll export failing", "open"),
        }
        self._next = 300

    def get(self, tenant_id: str, ticket_id: str) -> tuple[str, str] | None:
        record = self._tickets.get(ticket_id)
        if record is None or record[0] != tenant_id:
            return None  # another tenant's ticket is indistinguishable from a missing one
        return record[1], record[2]

    def create(self, tenant_id: str, title: str) -> str:
        ticket_id = f"T-{self._next}"
        self._next += 1
        self._tickets[ticket_id] = (tenant_id, title, "open")
        return ticket_id


KNOWLEDGE_BASE = {
    "password": "To reset your password, use 'Forgot password' on the sign-in page; a reset link is emailed to you.",
    "vpn": "If the VPN drops, update the client to the latest version and switch to the wired network.",
}


class SupportAgent:
    def __init__(self, tenant_id: str, planner: Planner, store: TicketStore, enforce_guardrails: bool = True):
        self.tenant_id = tenant_id
        self.planner = planner
        self.store = store
        self.enforce = enforce_guardrails
        self.tools = {
            tool.name: tool
            for tool in [
                Tool("search_kb", self._search_kb),
                Tool("get_ticket", self._get_ticket),
                Tool("create_ticket", self._create_ticket),
                Tool("close_ticket", self._close_ticket, needs_confirmation=True),
                Tool("send_email", self._send_email, needs_confirmation=True),
            ]
        }

    # --- tools -------------------------------------------------------------
    def _search_kb(self, query: str) -> str:
        hits = [text for key, text in KNOWLEDGE_BASE.items() if key in query.lower()]
        return hits[0] + " [source: KB]" if hits else ""

    def _get_ticket(self, ticket_id: str) -> str:
        found = self.store.get(self.tenant_id, ticket_id)
        return f"{ticket_id}: {found[0]} ({found[1]})" if found else ""

    def _create_ticket(self, title: str) -> str:
        return f"Created {self.store.create(self.tenant_id, title)}: {title}"

    def _close_ticket(self, ticket_id: str) -> str:
        return f"Closed {ticket_id}"

    def _send_email(self, to: str, body: str) -> str:
        return f"Emailed {to}"

    # --- runtime -----------------------------------------------------------
    def run(self, request: str, documents: list[str] | None = None) -> AgentResult:
        documents = documents or []
        text = CARD_NUMBER.sub("[REDACTED]", request) if self.enforce else request
        result = AgentResult(answer="", planner_input=text)
        outputs: list[str] = []

        for action in self.planner.plan(text, documents):
            tool = self.tools.get(action.tool)
            if self.enforce and (tool is None or action.source == "document"):
                result.blocked.append(action.tool)
                continue
            if tool is None:  # guardrails off: an unknown tool "runs"
                result.executed.append(action.tool)
                continue
            if self.enforce and tool.needs_confirmation:
                result.awaiting_confirmation.append(action.tool)
                continue
            result.executed.append(action.tool)
            outputs.append(tool.handler(**action.args))

        found = [o for o in outputs if o]
        if found:
            result.answer = " ".join(found)
        elif result.awaiting_confirmation:
            result.answer = f"Please confirm before I {', '.join(result.awaiting_confirmation)}."
        else:
            result.answer = "I don't know — I couldn't find that in the knowledge base or your tickets."
        return result


class RuleBasedPlanner:
    """Keyword planner standing in for an LLM. Naive on purpose: it obeys documents."""

    def plan(self, request: str, documents: list[str]) -> list[Action]:
        r = request.lower()
        actions: list[Action] = []
        ticket = re.search(r"\bT-\d+\b", request)
        if "delete" in r and ticket:
            actions.append(Action("delete_ticket", {"ticket_id": ticket.group()}))
        elif "close" in r and ticket:
            actions.append(Action("close_ticket", {"ticket_id": ticket.group()}))
        elif ticket:
            actions.append(Action("get_ticket", {"ticket_id": ticket.group()}))
        elif "open a ticket" in r or "create a ticket" in r:
            actions.append(Action("create_ticket", {"title": request.split(":", 1)[-1].strip()}))
        elif "email" in r:
            actions.append(Action("send_email", {"to": "manager@example.com", "body": request}))
        else:
            actions.append(Action("search_kb", {"query": request}))
        for doc in documents:
            match = re.search(r"email .* to (\S+@\S+)", doc, re.IGNORECASE)
            if match:
                actions.append(Action("send_email", {"to": match.group(1), "body": "all tickets"}, source="document"))
        return actions
