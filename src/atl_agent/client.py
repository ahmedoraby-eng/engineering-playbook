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
        """The issue's status: {"name": ..., "category": "new" | "indeterminate" | "done"}.
        Needs read:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status")
        status = data["fields"]["status"]
        return {"name": status["name"], "category": status["statusCategory"]["key"]}

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

    def jira_start(self, issue: str) -> str:
        """Pick up an issue: To Do -> In Progress, add the `agent-picked` label, return the new status.

        The only transition an agent makes on its own (Ahmed, 29/10/2026); Done comes from a merged PR
        with `Closes <KEY>`, and every other transition stays with Ahmed. Refuses an issue that is not in
        a To Do status or was already picked, so a second pickup can't happen.
        Needs read:jira-work and write:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,labels")
        fields = data["fields"]
        if PICKED_LABEL in fields.get("labels", []):
            raise AtlError(f"{issue}: already has the '{PICKED_LABEL}' label, not picking it up again")
        if fields["status"]["statusCategory"]["key"] != "new":
            raise AtlError(f"{issue}: status is '{fields['status']['name']}', an agent only starts issues in To Do")
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        options = [t for t in moves if t["to"]["statusCategory"]["key"] == "indeterminate"]
        if not options:
            raise AtlError(f"{issue}: no transition to an In Progress status is available")
        chosen = next((t for t in options if t["to"]["name"] == "In Progress"), options[0])
        self._call("PUT", "jira", f"/rest/api/3/issue/{issue}", {"update": {"labels": [{"add": PICKED_LABEL}]}})
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        return chosen["to"]["name"]

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
