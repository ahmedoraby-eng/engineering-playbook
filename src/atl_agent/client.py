"""Jira and Confluence writes as one agent's Atlassian account.

The agent's scoped API token is read from the macOS Keychain for each command (service
`basita-atl-<agent>-<jira|confluence>`, falling back to `basita-atl-<agent>`; account `basita`) and
kept only in memory: it is never printed, logged or written to disk. A scoped token is made for
one app, hence one item per product. Scoped tokens only work through the api.atlassian.com gateway, so every call
goes to `https://api.atlassian.com/ex/{jira|confluence}/<cloud id>/...` with basic auth
`<agent>@<domain>:<token>`.
"""

from __future__ import annotations

import base64
import json
import subprocess
import urllib.error
import urllib.request
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the CLI also runs on macOS's system Python 3.9
    from collections.abc import Callable

    # (method, url, headers, body) -> (status, response text). Swapped out in tests.
    Transport = Callable[[str, str, dict, bytes | None], tuple[int, str]]

AGENTS = ("talia", "malek", "salma", "sherif", "haya", "adam", "nora", "maya")
GATEWAY = "https://api.atlassian.com/ex"


class AtlError(Exception):
    """A failed call. The message never contains the token."""


def keychain_token(agent: str, product: str = "", run: Callable = subprocess.run) -> str:
    """The agent's token for a product from the login Keychain, or AtlError if there is none."""
    services = [f"basita-atl-{agent}-{product}", f"basita-atl-{agent}"] if product else [f"basita-atl-{agent}"]
    for service in services:
        result = run(
            ["security", "find-generic-password", "-s", service, "-a", "basita", "-w"],
            capture_output=True,
            text=True,
        )
        token = (result.stdout or "").strip()
        if result.returncode == 0 and token:
            return token
    raise AtlError(f"no Keychain item {' or '.join(services)} (account 'basita'); see the token trial runbook")


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


PICKED_LABEL = "agent-picked"
REFINING_LABEL = "agent-refining"
REVIEW_LABEL = "awaiting-approval"
# Workflow names on the Basita site (30/10/2026). DFP: Refinement -> Ready for Development -> In
# Development -> ... -> Ready for Acceptance -> Released. BASITA and ELB: To Do -> In Progress -> Done.
REFINEMENT_STATUS = "Refinement"
WORK_STATUSES = ("In Development", "In Progress")  # DFP has In Development, BASITA and ELB In Progress
READY_STATUSES = ("Ready for Development", "Ready", "To Do")
REVIEW_STATUSES = ("Ready for Acceptance", "In Review", "Awaiting Approval")


def _adf(text: str, mentions: tuple[str, ...] = ()) -> dict:
    """Plain text as an Atlassian Document Format document, one paragraph per line.
    Each account id in `mentions` becomes an @mention in a first paragraph, so Jira notifies that person."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} if line else {"type": "paragraph"}
        for line in text.splitlines() or [""]
    ]
    if mentions:
        nodes: list[dict] = []
        for account in mentions:
            nodes += [{"type": "mention", "attrs": {"id": account}}, {"type": "text", "text": " "}]
        paragraphs.insert(0, {"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": paragraphs}


class AtlClient:
    def __init__(
        self,
        agent: str,
        cloud_id: str,
        token: str,
        domain: str = "basitasolutions.com",
        transport: Transport = urllib_transport,
    ):
        agent = agent.lower()
        if agent not in AGENTS:
            raise AtlError(f"unknown agent '{agent}' (known: {', '.join(AGENTS)})")
        if not cloud_id:
            raise AtlError("no Atlassian cloud id: set ATL_CLOUD_ID or pass --cloud-id")
        self.agent = agent
        self.display = agent.capitalize()
        self.cloud_id = cloud_id
        self._auth = "Basic " + base64.b64encode(f"{agent}@{domain}:{token}".encode()).decode()
        self._transport = transport

    def __repr__(self) -> str:  # keep the token out of any accidental print
        return f"AtlClient(agent={self.agent!r}, cloud_id={self.cloud_id!r})"

    def _call(self, method: str, product: str, path: str, payload: dict | None = None) -> dict:
        url = f"{GATEWAY}/{product}/{self.cloud_id}{path}"
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        status, text = self._transport(method, url, headers, body)
        if not 200 <= status < 300:
            hint = {
                401: "token missing, expired or revoked, or the email does not match the token's account",
                403: "the token's scopes or the account's permissions do not allow this",
                404: "not found, or not visible to this account",
            }.get(status, "request failed")
            raise AtlError(f"{method} {product}{path}: HTTP {status} ({hint}): {text[:300]}")
        return json.loads(text) if text.strip() else {}

    def jira_comment(self, issue: str, text: str, mentions: tuple[str, ...] = ()) -> dict:
        """Add a comment to a Jira issue, @mentioning the given account ids. Needs write:comment:jira."""
        return self._call("POST", "jira", f"/rest/api/3/issue/{issue}/comment", {"body": _adf(text, mentions)})

    def jira_status(self, issue: str) -> dict:
        """The issue's status and labels: {"name", "category" ("new" | "indeterminate" | "done"), "labels"}.
        Needs read:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,labels")
        status = data["fields"]["status"]
        return {
            "name": status["name"],
            "category": status["statusCategory"]["key"],
            "labels": data["fields"].get("labels", []),
        }

    def jira_close(self, issue: str) -> str:
        """Move the issue to a status in the Done category and return that status's name.
        Needs read:jira-work and write:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions")
        options = [t for t in data.get("transitions", []) if t["to"]["statusCategory"]["key"] == "done"]
        if not options:
            raise AtlError(f"{issue}: no transition to a Done status is available from its current status")
        chosen = next((t for t in options if t["to"]["name"] == "Done"), options[0])
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        return chosen["to"]["name"]

    def _issue(self, issue: str) -> dict:
        return self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,labels")["fields"]

    def _transitions(self, issue: str) -> list[dict]:
        return self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])

    def _move(self, issue: str, transition: dict) -> str:
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": transition["id"]}})
        return transition["to"]["name"]

    def _labels(self, issue: str, add: str = "", remove: str = "") -> None:
        ops = ([{"add": add}] if add else []) + ([{"remove": remove}] if remove else [])
        self._call("PUT", "jira", f"/rest/api/3/issue/{issue}", {"update": {"labels": ops}})

    def jira_start(self, issue: str) -> str:
        """Start development: To Do / Ready for Development -> In Progress / In Development, add the
        `agent-picked` label, return the new status.

        One of the transitions an agent makes on its own (Ahmed, 29-30/10/2026); Done comes from a merged
        PR with `Closes <KEY>`. Refuses an issue already picked, one in Refinement (that is
        `jira_refine_start`), or one that is not in a To Do-category status, so a second pickup can't
        happen. Needs read:jira-work and write:jira-work."""
        fields = self._issue(issue)
        status = fields["status"]
        if PICKED_LABEL in fields.get("labels", []):
            raise AtlError(f"{issue}: already has the '{PICKED_LABEL}' label, not picking it up again")
        if status["name"] == REFINEMENT_STATUS:
            raise AtlError(f"{issue}: is in {REFINEMENT_STATUS}; refine it first (jira-refine-start)")
        if status["statusCategory"]["key"] != "new":
            raise AtlError(f"{issue}: status is '{status['name']}', an agent only starts issues that are ready")
        options = [t for t in self._transitions(issue) if t["to"]["statusCategory"]["key"] == "indeterminate"]
        chosen = next((t for name in WORK_STATUSES for t in options if t["to"]["name"] == name), None)
        if chosen is None:
            raise AtlError(f"{issue}: no transition to {' or '.join(WORK_STATUSES)} is available")
        self._labels(issue, add=PICKED_LABEL)
        return self._move(issue, chosen)

    def jira_refine_start(self, issue: str) -> None:
        """Start a refinement job: the issue stays in Refinement and gets the `agent-refining` label.
        Needs write:jira-work."""
        fields = self._issue(issue)
        if fields["status"]["name"] != REFINEMENT_STATUS:
            raise AtlError(f"{issue}: status is '{fields['status']['name']}', not {REFINEMENT_STATUS}")
        if REFINING_LABEL in fields.get("labels", []):
            raise AtlError(f"{issue}: already has the '{REFINING_LABEL}' label, not refining it again")
        self._labels(issue, add=REFINING_LABEL)

    def jira_refined(self, issue: str) -> str:
        """Finish a refinement job: Refinement -> Ready for Development (part of the agent's standing
        approval, Ahmed 30/10/2026), remove `agent-refining`. Needs write:jira-work."""
        fields = self._issue(issue)
        if fields["status"]["name"] != REFINEMENT_STATUS:
            raise AtlError(f"{issue}: status is '{fields['status']['name']}', not {REFINEMENT_STATUS}")
        moves = self._transitions(issue)
        chosen = next((t for name in READY_STATUSES for t in moves if t["to"]["name"] == name), None)
        if chosen is None:
            raise AtlError(f"{issue}: no transition to {' or '.join(READY_STATUSES)} is available")
        new_status = self._move(issue, chosen)
        self._labels(issue, remove=REFINING_LABEL)
        return new_status

    def jira_review(self, issue: str, approver: str) -> str:
        """Hand finished work to a person for approval (human in the loop, Ahmed 30/10/2026): move it to a
        review status where the workflow has one (DFP: Ready for Acceptance), always add the
        `awaiting-approval` label, and assign it to the approver. The issue is then not Done, not
        picked again, and does not unblock its dependants. Returns the status it is in.
        Needs read:jira-work and write:jira-work."""
        if not approver:
            raise AtlError(f"{issue}: an approver account id is needed")
        fields = self._issue(issue)
        status = fields["status"]["name"]
        if fields["status"]["statusCategory"]["key"] == "done":
            raise AtlError(f"{issue}: is already {status}")
        if status not in REVIEW_STATUSES:
            moves = self._transitions(issue)
            chosen = next((t for name in REVIEW_STATUSES for t in moves if t["to"]["name"] == name), None)
            if chosen is not None:
                status = self._move(issue, chosen)
        self._labels(issue, add=REVIEW_LABEL)
        self._call("PUT", "jira", f"/rest/api/3/issue/{issue}/assignee", {"accountId": approver})
        return status

    def jira_link(self, blocker: str, blocked: str) -> None:
        """Record that `blocker` blocks `blocked` (link type Blocks), so pickup waits for the blocker.
        In POST /issueLink the inward issue is the one that blocks. Needs write:issue-link:jira or
        write:jira-work and the project's Link Issues permission."""
        payload = {"type": {"name": "Blocks"}, "inwardIssue": {"key": blocker}, "outwardIssue": {"key": blocked}}
        self._call("POST", "jira", "/rest/api/3/issueLink", payload)

    def confluence_append(self, page_id: str, text: str) -> dict:
        """Append a paragraph to a Confluence page, with a version note naming the agent.
        Needs the Confluence scopes read:page:confluence and write:page:confluence."""
        page = self._call("GET", "confluence", f"/wiki/api/v2/pages/{page_id}?body-format=storage")
        current = page["body"]["storage"]["value"]
        payload = {
            "id": page["id"],
            "status": "current",
            "title": page["title"],
            "body": {"representation": "storage", "value": current + f"<p>{escape(text)}</p>"},
            "version": {"number": page["version"]["number"] + 1, "message": f"[{self.display}] {text[:200]}"},
        }
        return self._call("PUT", "confluence", f"/wiki/api/v2/pages/{page_id}", payload)
