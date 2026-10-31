"""Jira reads, and Jira and Confluence writes, as one agent's Atlassian account.

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
import os
import re
import subprocess
import urllib.error
import urllib.request
from html import escape
from typing import TYPE_CHECKING
from urllib.parse import quote

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
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")


_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>\[\]()]+[^\s<>\[\]().,;:!?\"'])")


def _inline(line: str) -> list[dict]:
    """A line as ADF text nodes in which `[label](url)` and bare http(s) URLs are clickable links (§4.4 item 5)."""
    nodes: list[dict] = []
    position = 0
    for match in _LINK.finditer(line):
        if match.start() > position:
            nodes.append({"type": "text", "text": line[position : match.start()]})
        label, url = (match.group(1), match.group(2)) if match.group(2) else (match.group(3), match.group(3))
        nodes.append({"type": "text", "text": label, "marks": [{"type": "link", "attrs": {"href": url}}]})
        position = match.end()
    if position < len(line):
        nodes.append({"type": "text", "text": line[position:]})
    return nodes


def _adf(text: str, mentions: tuple[str, ...] = ()) -> dict:
    """Plain text as an Atlassian Document Format document, one paragraph per line.
    Each account id in `mentions` becomes an @mention in a first paragraph, so Jira notifies that person."""
    paragraphs = [
        {"type": "paragraph", "content": _inline(line)} if line else {"type": "paragraph"}
        for line in text.splitlines() or [""]
    ]
    if mentions:
        nodes: list[dict] = []
        for account in mentions:
            nodes += [{"type": "mention", "attrs": {"id": account}}, {"type": "text", "text": " "}]
        paragraphs.insert(0, {"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _adf_text(node: dict | None) -> str:
    """An Atlassian Document Format node as plain text, keeping link targets and mentions."""
    if not node:
        return ""
    kind = node.get("type")
    if kind == "text":
        text = node.get("text", "")
        href = next((m["attrs"].get("href") for m in node.get("marks", []) if m.get("type") == "link"), None)
        return f"{text} ({href})" if href and href != text else text
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return node.get("attrs", {}).get("text", "@?")
    if kind in ("inlineCard", "blockCard"):
        return node.get("attrs", {}).get("url", "")
    children = node.get("content", [])
    inline = kind in ("paragraph", "heading")
    text = ("" if inline else "\n").join(_adf_text(child) for child in children)
    return f"- {text}" if kind == "listItem" else text


# Basita's Atlassian site (basitasolutions.atlassian.net). The cloud id is an identifier, not a secret.
# ATL_CLOUD_ID or --cloud-id override it, so an autonomous session needs no environment setup.
DEFAULT_CLOUD_ID = "67176086-263c-46b2-8f6d-167a10620df4"


def default_cloud_id(environ: dict | None = None) -> str:
    return ((environ if environ is not None else os.environ).get("ATL_CLOUD_ID") or "").strip() or DEFAULT_CLOUD_ID


# The status a refined issue moves to: BASITA/ELB call it Ready, DFP calls it Ready for Development.
READY_STATUSES = ("ready", "ready for development")
# DFP queues (operating model 4.3 items 11 and 12): a `Ready for ...` status is a queue, an `In ...` status
# means someone is working. Salma pulls only from Ready for Testing, into In Testing, and no other agent
# pulls from it; in DFP she pulls from nothing else.
TESTING_QUEUE = "ready for testing"
CLOSE_REASONS = ("Not a Bug", "Already Fixed", "Duplicate", "Business Requirement")
SALMA_ACCOUNT = "712020:a11d40c9-5c21-4caf-8743-b118b75cd9d2"


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

    def jira_read(self, issue: str) -> dict:
        """An issue's fields, comments and linked issues, read as this agent. Only makes GET calls.
        `assigned_to_reader` comes from a currentUser() search, which shows whose token this is, since these
        tokens can't call /myself. Needs read:jira-work."""
        if not ISSUE_KEY.match(issue):
            raise AtlError(f"'{issue}' is not an issue key")
        fields = "summary,status,issuetype,labels,assignee,reporter,priority,parent,description,issuelinks"
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields={fields}")["fields"]
        comments: list[dict] = []
        while True:
            page = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/comment?startAt={len(comments)}&maxResults=100")
            found = page.get("comments", [])
            comments += [
                {
                    "author": (c.get("author") or {}).get("displayName"),
                    "created": c.get("created"),
                    "text": _adf_text(c.get("body")),
                }
                for c in found
            ]
            if not found or len(comments) >= page.get("total", 0):
                break
        links = []
        for link in data.get("issuelinks", []):
            outward = "outwardIssue" in link
            other = link["outwardIssue"] if outward else link["inwardIssue"]
            links.append(
                {
                    "relation": link["type"]["outward" if outward else "inward"],
                    "key": other["key"],
                    "summary": other["fields"]["summary"],
                    "status": other["fields"]["status"]["name"],
                }
            )
        jql = quote(f"key = {issue} AND assignee = currentUser()")
        mine = self._call("GET", "jira", f"/rest/api/3/search/jql?jql={jql}&fields=key")
        name = lambda field: (data.get(field) or {}).get("displayName")  # noqa: E731
        return {
            "key": issue,
            "read_by": self.display,
            "assigned_to_reader": bool(mine.get("issues")),
            "summary": data.get("summary"),
            "status": data["status"]["name"],
            "type": data["issuetype"]["name"],
            "priority": (data.get("priority") or {}).get("name"),
            "assignee": name("assignee"),
            "reporter": name("reporter"),
            "labels": data.get("labels", []),
            "parent": (data.get("parent") or {}).get("key"),
            "description": _adf_text(data.get("description")),
            "links": links,
            "comments": comments,
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

    def jira_start(self, issue: str) -> str:
        """Pick up an issue: To Do -> In Progress, add the `agent-picked` label, return the new status.

        The only transition an agent makes on its own (Ahmed, 29/10/2026); Done comes from a merged PR
        with `Closes <KEY>`, and every other transition stays with Ahmed. Refuses an issue that is not in
        a To Do status or was already picked, so a second pickup can't happen.
        Needs read:jira-work and write:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,labels")
        fields = data["fields"]
        status = fields["status"]["name"]
        in_testing_queue = status.lower() == TESTING_QUEUE
        if PICKED_LABEL in fields.get("labels", []):
            raise AtlError(f"{issue}: already has the '{PICKED_LABEL}' label, not picking it up again")
        if in_testing_queue and self.agent != "salma":
            raise AtlError(f"{issue}: '{status}' is Salma's queue, {self.display} does not pull from it")
        if self.agent == "salma" and issue.startswith("DFP-") and not in_testing_queue:
            raise AtlError(f"{issue}: in DFP Salma pulls only from 'Ready for Testing', not '{status}'")
        if not in_testing_queue and fields["status"]["statusCategory"]["key"] != "new":
            raise AtlError(f"{issue}: status is '{status}', an agent only starts issues in To Do")
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        if in_testing_queue:
            chosen = next((t for t in moves if t["to"]["name"].lower() == "in testing"), None)
            if not chosen:
                raise AtlError(f"{issue}: no transition to In Testing is available")
        else:
            options = [
                t
                for t in moves
                if t["to"]["statusCategory"]["key"] == "indeterminate" and t["to"]["name"].lower() != "in testing"
            ]
            if not options:
                raise AtlError(f"{issue}: no transition to an In Progress status is available")
            chosen = next((t for t in options if t["to"]["name"] in ("In Progress", "In Development")), options[0])
        self._call("PUT", "jira", f"/rest/api/3/issue/{issue}", {"update": {"labels": [{"add": PICKED_LABEL}]}})
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        return chosen["to"]["name"]

    def jira_ready(self, issue: str) -> str:
        """Refinement -> Ready (or Ready for Development in DFP), the one transition an agent makes on a
        refined issue (Ahmed, 30/10/2026).

        Refuses an Epic (never worked as a ticket) and any issue that is not in Refinement, and only ever
        takes a transition that lands on one of READY_STATUSES. Needs read:jira-work and write:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,issuetype")
        fields = data["fields"]
        if fields["issuetype"]["name"] == "Epic":
            raise AtlError(f"{issue}: an Epic is never worked as a ticket")
        if fields["status"]["name"].lower() != "refinement":
            current = fields["status"]["name"]
            raise AtlError(f"{issue}: status is '{current}', an agent only readies issues in Refinement")
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        chosen = next((t for t in moves if t["to"]["name"].lower() in READY_STATUSES), None)
        if not chosen:
            raise AtlError(f"{issue}: no transition to Ready or Ready for Development is available from Refinement")
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        return chosen["to"]["name"]

    def jira_return(self, issue: str) -> str:
        """Adam's return path (4.3 item 11): Refinement -> Ready for Testing, assigned to Salma. The
        reason is the comment the CLI posts. It can only leave Refinement, and only Adam may use it.
        Needs read:jira-work and write:jira-work."""
        if self.agent != "adam":
            raise AtlError("jira-return is Adam's: only the Product Owner returns an issue from Refinement")
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,issuetype")
        fields = data["fields"]
        if fields["issuetype"]["name"] == "Epic":
            raise AtlError(f"{issue}: an Epic is never worked as a ticket")
        if fields["status"]["name"].lower() != "refinement":
            current = fields["status"]["name"]
            raise AtlError(f"{issue}: status is '{current}', jira-return only leaves Refinement")
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        chosen = next((t for t in moves if t["to"]["name"].lower() == TESTING_QUEUE), None)
        if not chosen:
            raise AtlError(f"{issue}: no transition to Ready for Testing is available from Refinement")
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        self._call("PUT", "jira", f"/rest/api/3/issue/{issue}/assignee", {"accountId": SALMA_ACCOUNT})
        return chosen["to"]["name"]

    def jira_dismiss(self, issue: str, reason: str) -> str:
        """Salma's closing (4.3 item 12): In Testing -> Closed with one of CLOSE_REASONS (the CLI posts
        it as the comment). Only Salma, only from In Testing. Needs read:jira-work and write:jira-work."""
        if self.agent != "salma":
            raise AtlError("jira-dismiss is Salma's: QA closes a bug, as QA opened it")
        if reason not in CLOSE_REASONS:
            raise AtlError(f"reason must be one of: {', '.join(CLOSE_REASONS)}")
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status")
        current = data["fields"]["status"]["name"]
        if current.lower() != "in testing":
            raise AtlError(f"{issue}: status is '{current}', jira-dismiss only closes from In Testing")
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        chosen = next((t for t in moves if t["to"]["name"].lower() == "closed"), None)
        if not chosen:
            raise AtlError(f"{issue}: no transition to Closed is available from In Testing")
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
