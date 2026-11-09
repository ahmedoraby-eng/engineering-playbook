"""Jira and Confluence reads and writes, as one agent's Atlassian account. Nothing here deletes.

The agent's scoped API token is read from the macOS Keychain for each command (service
`basita-atl-<agent>-<jira|confluence>`, falling back to `basita-atl-<agent>`; account `basita`) and
kept only in memory: it is never printed, logged or written to disk. A scoped token is made for
one app, hence one item per product. Scoped tokens only work through the api.atlassian.com gateway, so every call
goes to `https://api.atlassian.com/ex/{jira|confluence}/<cloud id>/...` with basic auth
`<agent>@<domain>:<token>`.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from . import snapshot as snap

if TYPE_CHECKING:  # the CLI also runs on macOS's system Python 3.9
    from collections.abc import Callable

    # (method, url, headers, body) -> (status, response text). Swapped out in tests.
    Transport = Callable[[str, str, dict, bytes | None], tuple[int, str]]

AGENTS = ("talia", "malek", "salma", "sherif", "haya", "adam", "nora", "maya", "yousef", "basel")
GATEWAY = "https://api.atlassian.com/ex"
_UTC = datetime.timezone.utc  # noqa: UP017 - datetime.UTC is Python 3.11, and the CLI also runs on macOS's 3.9


class AtlError(Exception):
    """A failed call. The message never contains the token."""


class LinkCommentError(AtlError):
    """jira_md wrote the snapshot at `path`, but its link comment failed. A read can still use the file."""

    def __init__(self, message: str, path: str):
        super().__init__(message)
        self.path = path


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
_PAGE_ID = re.compile(r"^(?:.*/pages/)?([0-9]+)(?:[/?#].*)?$")
_SPACE_KEY = re.compile(r"^~?[A-Za-z0-9_-]+$")


_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)|(https?://[^\s<>\[\]()]+[^\s<>\[\]().,;:!?\"'])")


def _with_mentions(text: str) -> list[dict]:
    """Plain text as ADF nodes in which `@Name` is a real mention of anyone with a Jira seat. Others stay as text:
    `@Talia` still counts in the comment relay, which reads the text."""
    pattern = r"(?<![\w.@/])@(" + "|".join(JIRA_ACCOUNTS) + r")\b"
    nodes: list[dict] = []
    position = 0
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        if match.start() > position:
            nodes.append({"type": "text", "text": text[position : match.start()]})
        name = match.group(1).lower()
        nodes.append({"type": "mention", "attrs": {"id": JIRA_ACCOUNTS[name], "text": f"@{name.capitalize()}"}})
        position = match.end()
    if position < len(text):
        nodes.append({"type": "text", "text": text[position:]})
    return nodes


def _inline(line: str) -> list[dict]:
    """A line as ADF nodes in which `[label](url)` and bare http(s) URLs are clickable links (§4.4 item 5), and
    `@Name` is a mention."""
    nodes: list[dict] = []
    position = 0
    for match in _LINK.finditer(line):
        if match.start() > position:
            nodes += _with_mentions(line[position : match.start()])
        label, url = (match.group(1), match.group(2)) if match.group(2) else (match.group(3), match.group(3))
        nodes.append({"type": "text", "text": label, "marks": [{"type": "link", "attrs": {"href": url}}]})
        position = match.end()
    if position < len(line):
        nodes += _with_mentions(line[position:])
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
    separator = {"paragraph": "", "heading": "", "tableRow": " | ", "tableCell": " ", "tableHeader": " "}.get(
        kind, "\n"
    )
    text = separator.join(_adf_text(child) for child in children)
    return f"- {text}" if kind == "listItem" else text


# ADF -> Markdown (BASITA-205, ADR-B001). Inline nodes are rendered into a line of text; everything else is a block.
_MD_INLINE = ("text", "hardBreak", "mention", "emoji", "inlineCard", "status", "date", "placeholder", "mediaInline")
# What an unknown node with no children of its own still shows, in this order, so it is never dropped.
_MD_FALLBACK_ATTRS = ("text", "url", "shortName", "alt", "title")


def _md_code_span(text: str) -> str:
    """Text as an inline code span, with a fence longer than any run of backticks inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    padded = f" {text} " if text.startswith("`") or text.endswith("`") else text
    return f"{fence}{padded}{fence}"


def _md_text(node: dict) -> str:
    """A text node with its marks. Spaces at either end stay outside the markers, which Markdown needs."""
    text = node.get("text", "")
    core = text.strip()
    if not core:
        return text
    lead, trail = text[: len(text) - len(text.lstrip())], text[len(text.rstrip()) :]
    marks = {m.get("type"): m for m in node.get("marks", []) if isinstance(m, dict)}
    if "code" in marks:
        core = _md_code_span(core)
    if "strike" in marks:
        core = f"~~{core}~~"
    if "em" in marks:
        core = f"_{core}_"
    if "strong" in marks:
        core = f"**{core}**"
    href = (marks.get("link") or {}).get("attrs", {}).get("href")
    if href:
        target = f"<{href}>" if re.search(r"[\s()<>]", href) else href
        core = href if core == href else f"[{core}]({target})"
    return f"{lead}{core}{trail}"


def _md_fallback(node: dict) -> str:
    attrs = node.get("attrs") or {}
    return next((str(attrs[name]) for name in _MD_FALLBACK_ATTRS if attrs.get(name)), "")


def _md_inline(nodes: list) -> str:
    """Inline nodes as one piece of Markdown text."""
    parts = []
    for node in nodes:
        kind = node.get("type")
        attrs = node.get("attrs") or {}
        if kind == "text":
            parts.append(_md_text(node))
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind == "mention":
            parts.append(attrs.get("text") or "@?")
        elif kind == "emoji":
            parts.append(attrs.get("shortName") or attrs.get("text") or "")
        elif kind in ("inlineCard", "blockCard", "embedCard"):
            parts.append(attrs.get("url") or "")
        elif kind == "status":
            parts.append(attrs.get("text") or "")
        elif kind == "date" and str(attrs.get("timestamp", "")).isdigit():
            moment = datetime.datetime.fromtimestamp(int(attrs["timestamp"]) / 1000, _UTC)
            parts.append(moment.strftime("%Y-%m-%d"))
        elif node.get("content"):
            parts.append(_md_inline(node["content"]))
        else:
            parts.append(_md_fallback(node))
    return "".join(parts)


def _md_indent(text: str, prefix: str, first: str | None = None, blank: str = "") -> str:
    """Every line of `text` after `prefix`, the first one after `first` when given; an empty line becomes `blank`."""
    lines = text.split("\n")
    head = (first if first is not None else prefix) + lines[0]
    return "\n".join([head] + [prefix + line if line else blank for line in lines[1:]])


def _md_list(node: dict, offset: int) -> str:
    """A bullet or ordered list. A nested list is indented by its parent item's marker, as CommonMark reads it."""
    ordered = node.get("type") == "orderedList"
    number = int((node.get("attrs") or {}).get("order") or 1)
    items = []
    for item in node.get("content", []):
        marker = f"{number}. " if ordered else "- "
        number += 1
        body = "\n".join(_md_blocks(item.get("content", []), offset)) if item.get("content") else ""
        items.append(_md_indent(body, " " * len(marker), marker))
    return "\n".join(items)


def _md_tasks(node: dict) -> str:
    """A Jira action list: `- [ ]` or `- [x]` items, a nested list indented under its item."""
    lines = []
    for child in node.get("content", []):
        if child.get("type") == "taskItem":
            done = (child.get("attrs") or {}).get("state") == "DONE"
            lines.append(f"- [{'x' if done else ' '}] " + _md_inline(child.get("content", [])))
        elif child.get("type") == "taskList":
            lines.append(_md_indent(_md_tasks(child), "  "))
        else:
            lines.append(_md_indent("\n".join(_md_blocks([child])), "  "))
    return "\n".join(lines)


def _md_cell(cell: dict, offset: int) -> str:
    """A table cell on one line: its blocks joined by <br>, and `|` escaped so it can't split the cell."""
    text = "<br>".join(_md_blocks(cell.get("content", []), offset))
    return text.replace("|", "\\|").replace("\n", "<br>").strip()


def _md_table(node: dict, offset: int) -> str:
    """A pipe table. The first row is the header, as Markdown needs one; short rows are padded."""
    rows = [
        [_md_cell(cell, offset) for cell in row.get("content", [])]
        for row in node.get("content", [])
        if row.get("type") == "tableRow"
    ]
    if not rows:
        return ""
    width = max(len(row) for row in rows) or 1
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in rows]
    lines.insert(1, "|" + "|".join([" --- "] * width) + "|")
    return "\n".join(lines)


def _md_block(node: dict, offset: int) -> str:
    """One block node as Markdown."""
    kind = node.get("type")
    children = node.get("content", [])
    if kind == "paragraph":
        return _md_inline(children)
    if kind == "heading":
        level = min(6, int((node.get("attrs") or {}).get("level") or 1) + offset)
        return "#" * level + " " + _md_inline(children)
    if kind in ("bulletList", "orderedList"):
        return _md_list(node, offset)
    if kind == "taskList":
        return _md_tasks(node)
    if kind == "codeBlock":
        code = "".join(child.get("text", "") for child in children)
        longest = max((len(run) for run in re.findall(r"`{3,}", code)), default=2)
        fence = "`" * (longest + 1)
        return f"{fence}{(node.get('attrs') or {}).get('language') or ''}\n{code}\n{fence}"
    if kind in ("blockquote", "panel"):
        return _md_indent("\n\n".join(_md_blocks(children, offset)), "> ", blank=">")
    if kind == "rule":
        return "---"
    if kind == "table":
        return _md_table(node, offset)
    if kind in ("blockCard", "embedCard"):
        return (node.get("attrs") or {}).get("url") or ""
    if kind in ("expand", "nestedExpand"):
        title = (node.get("attrs") or {}).get("title")
        return "\n\n".join(([f"**{title}**"] if title else []) + _md_blocks(children, offset))
    if children:  # unknown: its children, never dropped
        return "\n\n".join(_md_blocks(children, offset))
    return _md_fallback(node)


def _md_blocks(nodes: list, offset: int = 0) -> list[str]:
    """Block nodes as Markdown blocks, empty ones left out. Inline nodes found among blocks make one paragraph."""
    blocks: list[str] = []
    run: list[dict] = []
    for node in [*nodes, None]:
        if node is not None and node.get("type") in _MD_INLINE:
            run.append(node)
            continue
        if run:
            blocks.append(_md_inline(run))
            run = []
        if node is not None:
            blocks.append(_md_block(node, offset))
    return [block for block in blocks if block.strip()]


def _adf_md(node: dict | None, heading_offset: int = 0) -> str:
    """An Atlassian Document Format node as Markdown (BASITA-205): headings, nested lists, code blocks, quotes and
    panels, rules, pipe tables, and the inline marks, links, mentions, cards, emoji and statuses. An unknown node
    shows its children's text. `heading_offset` moves headings down, so a description's `#` sits under the file's own
    sections."""
    if not node:
        return ""
    return "\n\n".join(_md_blocks([node], heading_offset)).strip()


# jira-md (BASITA-205, ADR-B001): a Markdown read copy of an issue at <out>/work/jira/<KEY>.md. Jira stays the master.
SITE_URL = "https://basitasolutions.atlassian.net"
SNAPSHOT_FOLDER = ("work", "jira")
# A snapshot never goes into the app repository (AC4): its origin URL holds this name.
APP_REPO = "elbayoumy-farm-nutrition-platform"
_SNAPSHOT_HEADER = re.compile(r"^<!-- jira-md: \S+ · updated (\S+) · fetched \S+ -->$")
SNAPSHOT_FIELDS = (
    "summary,status,issuetype,priority,assignee,reporter,labels,parent,fixVersions,description,issuelinks,subtasks,"
    "updated,created,duedate,resolution,components,attachment"
)


def comment_limit(value: str) -> int | None:
    """jira-get --comments: None for "all", else how many of the latest comments (0 or more)."""
    if str(value).lower() == "all":
        return None
    if not str(value).isdigit():
        raise AtlError(f"--comments takes all or a number, not '{value}'")
    return int(value)


def snapshot_marker(issue: str) -> str:
    """The text that shows an issue already has its link comment."""
    return f"work/jira/{issue}.md"


def snapshot_comment(issue: str) -> str:
    """The one comment that tells agents where the issue's snapshot is (ADR-B001 decision 5)."""
    return (
        f"ملف التيكت للـ agents: basita-ops/{snapshot_marker(issue)} (يتحدث تلقائياً؛ Jira هو المرجع) "
        f"https://github.com/basitasolutions/basita-ops/blob/main/{snapshot_marker(issue)}"
    )


def refuse_app_repo(folder: Path, run: Callable = subprocess.run) -> None:
    """AtlError when `folder`, or any folder above it, is a git repository whose origin is the app repository.
    No git, no repository and no origin are all allowed."""
    env = {name: value for name, value in os.environ.items() if name not in ("GIT_DIR", "GIT_WORK_TREE")}
    for place in [folder, *folder.parents]:
        if not (place / ".git").exists():
            continue
        try:
            result = run(
                ["git", "-C", str(place), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except OSError:  # no git on this machine
            return
        except subprocess.SubprocessError as error:
            raise AtlError(f"could not tell whether {place} is the app repository: {error}") from None
        if result.returncode == 0 and APP_REPO in (result.stdout or "").lower():
            raise AtlError(f"{place} is the app repository ({APP_REPO}): a Jira snapshot never goes there (AC4)")


def _snapshot_updated(path: Path) -> str | None:
    """The `updated` an existing snapshot recorded in its header, or None."""
    try:
        with path.open(encoding="utf-8") as handle:
            head = [handle.readline().rstrip("\n") for _ in range(3)]
    except OSError:
        return None
    match = next((m for m in map(_SNAPSHOT_HEADER.match, head) if m), None)
    return match.group(1) if match else None


def _one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def _write_atomically(path: Path, text: str) -> None:
    """Write through a temporary file in the same folder, then replace, so a reader never sees half a file."""
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _snapshot_top(issue: str, fields: dict, fetched: str, legacy: bool = False) -> list[str]:
    """The top of the core file, up to the attachments: title, source line, field list, then the sections that have
    something in them. `legacy` gives the top of the file before BASITA-219 (no Components, Created, Due date,
    Resolution or Attachments, a plain Links heading), only to measure the core's size budget."""
    name = lambda field: (fields.get(field) or {}).get("displayName")  # noqa: E731
    parent = fields.get("parent") or {}
    parent_summary = _one_line((parent.get("fields") or {}).get("summary"))
    listed = [
        ("Status", (fields.get("status") or {}).get("name")),
        ("Type", (fields.get("issuetype") or {}).get("name")),
        ("Priority", (fields.get("priority") or {}).get("name")),
        ("Assignee", name("assignee") or "Unassigned"),
        ("Reporter", name("reporter")),
        ("Fix versions", ", ".join(v.get("name", "") for v in fields.get("fixVersions") or [])),
        ("Labels", ", ".join(fields.get("labels") or [])),
        ("Parent", f"{parent['key']}: {parent_summary}" if parent.get("key") and parent_summary else parent.get("key")),
        ("Components", ", ".join(c.get("name", "") for c in fields.get("components") or [])),
        ("Created", fields.get("created")),
        ("Due date", fields.get("duedate")),
        ("Resolution", (fields.get("resolution") or {}).get("name")),
    ][: 8 if legacy else None]
    lines = [
        f"# {issue}: {_one_line(fields.get('summary'))}",
        "",
        f"<!-- jira-md: {SITE_URL}/browse/{issue} · updated {fields.get('updated')} · fetched {fetched} -->",
        "",
        *[f"- **{label}:** {value}" for label, value in listed if value],
    ]
    description = _adf_md(fields.get("description"), heading_offset=2)
    if description:
        lines += ["", "## Description", "", description]
    links = []
    for link in fields.get("issuelinks", []):
        outward = "outwardIssue" in link
        other = link["outwardIssue"] if outward else link["inwardIssue"]
        relation = link["type"]["outward" if outward else "inward"]
        status = other["fields"]["status"]["name"]
        links.append(f"- {relation} {other['key']}: {_one_line(other['fields'].get('summary'))} ({status})")
    if links:
        # The other issue's status is the one read at `fetched` (header line); `updated` of this issue does not move
        # when a linked issue does, so the heading says so (Yousef's review of PR #52, non-blocking a).
        lines += ["", "## Links" if legacy else f"## Links (status as of {fetched})", "", *links]
    subtasks = [
        f"- {s['key']}: {_one_line(s['fields'].get('summary'))} ({s['fields']['status']['name']})"
        for s in fields.get("subtasks", [])
    ]
    if subtasks:
        lines += ["", "## Sub-tasks", "", *subtasks]
    attachments = [
        f"- {a.get('filename')} ({a.get('mimeType')}, {a.get('size')} bytes, "
        f"{(a.get('author') or {}).get('displayName') or 'Unknown'}, {a.get('created')})"
        for a in fields.get("attachment") or []
    ]
    if attachments and not legacy:
        lines += ["", "## Attachments (names only; contents are not downloaded)", "", *attachments]
    return lines


def _snapshot_files(
    issue: str, fields: dict, comments: list[dict], histories: list[dict], fetched: str
) -> tuple[str, str]:
    """The core file and the archive (BASITA-219): see snapshot.py for what each holds and how comments are chosen."""
    marker = f"<!-- jira-md: {SITE_URL}/browse/{issue} · updated {fields.get('updated')} · fetched {fetched} -->"
    records = [
        {
            "id": str(c.get("id")),
            "created": c.get("created"),
            "updated": c.get("updated"),
            "author": (c.get("author") or {}).get("displayName") or "Unknown",
            "account": (c.get("author") or {}).get("accountId"),
            "text": _adf_md(c.get("body"), heading_offset=3),
        }
        for c in comments
    ]
    records.sort(key=lambda c: (c["created"] or "", c["id"]))
    people = {"ahmed": {AHMED_ACCOUNT}, "adam": {ADAM_ACCOUNT}}
    chosen = snap.select_comments(records, people, snapshot_marker(issue))
    entries = snap.history_entries(histories)
    archive, ranges = snap.render_archive(issue, marker, records, entries)
    budget = snap.legacy_size(_snapshot_top(issue, fields, fetched, legacy=True), records)
    top = _snapshot_top(issue, fields, fetched)
    core, _ = snap.render_core(top, records, chosen, ranges, issue, entries, budget=budget)
    return core, archive


def _doc_text(item: dict) -> str:
    """The plain text of a Confluence page or comment asked for with body-format=atlas_doc_format."""
    value = ((item.get("body") or {}).get("atlas_doc_format") or {}).get("value") or ""
    try:
        return _adf_text(json.loads(value)) if value else ""
    except json.JSONDecodeError:
        return value


def _version_at(item: dict) -> str | None:
    return (item.get("version") or {}).get("createdAt")


def _page_body(text: str, storage: bool) -> dict:
    """A page body: plain text with clickable links, or Confluence storage format as given (for tables and macros)."""
    if storage:
        return {"representation": "storage", "value": text}
    return {"representation": "atlas_doc_format", "value": json.dumps(_adf(text))}


# Basita's Atlassian site (basitasolutions.atlassian.net). The cloud id is an identifier, not a secret.
# ATL_CLOUD_ID or --cloud-id override it, so an autonomous session needs no environment setup.
DEFAULT_CLOUD_ID = "67176086-263c-46b2-8f6d-167a10620df4"


def default_cloud_id(environ: dict | None = None) -> str:
    return ((environ if environ is not None else os.environ).get("ATL_CLOUD_ID") or "").strip() or DEFAULT_CLOUD_ID


# The status a refined issue moves to: BASITA/ELB call it Ready, DFP calls it Ready for Development.
READY_STATUSES = ("ready", "ready for development")
READY_NOT_FOR = ("adam", "salma")  # pickup doesn't start them on Ready for Development
# DFP queues (operating model 4.3 items 11 and 12): a `Ready for ...` status is a queue, an `In ...` status
# means someone is working. Salma pulls only from Ready for Testing, into In Testing, and no other agent
# pulls from it; in DFP she pulls from nothing else.
TESTING_QUEUE = "ready for testing"
CLOSE_REASONS = ("Not a Bug", "Already Fixed", "Duplicate", "Business Requirement")
SALMA_ACCOUNT = "712020:a11d40c9-5c21-4caf-8743-b118b75cd9d2"
ADAM_ACCOUNT = "712020:74556401-e243-4c53-9f59-75f28fdf6cec"
AHMED_ACCOUNT = "712020:710281c6-e4c2-44fd-b72a-81091935ae21"
ENGINEER_ACCOUNTS = (
    "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3",  # Malek
    "712020:dd45049f-7336-4c37-9f99-e8665102ed92",  # Haya
)
LAST_ENGINEER = "last engineer"
CREATE_PROJECTS = ("BASITA", "DFP", "ELB")
# What an agent writes -> Jira's name for the type. These projects call it Subtask.
CREATE_TYPES = {"Story": "Story", "Task": "Task", "Bug": "Bug", "Sub-task": "Subtask", "Subtask": "Subtask"}
BUG_ENGINEERS = ("malek", "haya")
BUG_DIRECT_STATUS = "Ready for Development"
EPIC_CREATORS = {"nora": CREATE_PROJECTS, "sherif": ("BASITA",)}
ASSIGNERS = ("adam", "nora", "sherif", "salma")
# People and agents with a Jira seat. Maya has none (her work goes to Sherif with the label agent-maya); Basel and Talia
# have no user.
JIRA_ACCOUNTS = {
    "malek": ENGINEER_ACCOUNTS[0],
    "haya": ENGINEER_ACCOUNTS[1],
    "salma": SALMA_ACCOUNT,
    "adam": ADAM_ACCOUNT,
    "sherif": "712020:06afd42e-fdc7-46be-877f-1f00f3770e81",
    "nora": "712020:05c4e58f-5b7e-482b-b3b6-01b027fd9e11",
    "yousef": "712020:b3f8403e-5f97-4351-bd9e-1435a3081131",
    "ahmed": AHMED_ACCOUNT,
    "yamen": "712020:135e8ec3-e174-46bf-b4fb-65de538cff2d",
    "abdelrahman": "712020:6f47dac6-82b6-4fd4-bd86-da7394ffa87d",
}
# Labels only Ahmed sets or removes, refused in any case; a whole label list must keep them exactly, case included
# (BASITA-169). jira-status-waived lets a PR past elbayoumy's Jira status
# check (BASITA-168).
AHMED_ONLY_LABELS = ("reviewed-by-ahmed", "human-reviewed", "needs-ahmed", "jira-status-waived")
PRIORITIES = ("Highest", "High", "Medium", "Low", "Lowest")
# Who works a DFP issue and submits it to testing (BASITA-147): the engineers, and the leads who take DFP work that
# isn't code. Only Malek and Haya could on 02/11/2026, so Nora left DFP-97 In Development. Maya has no Jira user and
# works through Sherif's.
SUBMITTERS = ("malek", "haya", "nora", "sherif", "maya")
# A failed test or a rejection goes back to the last of them who held the issue.
SUBMITTER_ACCOUNTS = tuple(dict.fromkeys(JIRA_ACCOUNTS["sherif" if name == "maya" else name] for name in SUBMITTERS))
# DFP's moves between queues (Ahmed, 01/11/2026): action -> who may run it, from, to, and the next assignee.
# A step in the flow is a row here; `jira-flow` refuses everything else. `Released` stays with the merge (close.py).
FLOW = {
    "submit": {
        "agents": SUBMITTERS,
        "from": "In Development",
        "to": "Ready for Testing",
        "assign": SALMA_ACCOUNT,
    },
    "pass": {"agents": ("salma",), "from": "In Testing", "to": "Ready for Acceptance", "assign": ADAM_ACCOUNT},
    "fail": {"agents": ("salma",), "from": "In Testing", "to": "Ready for Development", "assign": LAST_ENGINEER},
    "accept": {"agents": ("adam",), "from": "Ready for Acceptance", "to": "Ready for Release", "assign": AHMED_ACCOUNT},
    "reject": {
        "agents": ("adam",),
        "from": "Ready for Acceptance",
        "to": "Ready for Development",
        "assign": LAST_ENGINEER,
    },
    # BASITA-151 (plan 4.3, T3): the architect approves a reviewed PR's issue into testing. Off until Ahmed switches it
    # on (FLOW_APPROVE), and D6 decides who owns this step before that; Yousef is the plan's candidate.
    "approve": {
        "agents": ("yousef",),
        "from": "In Development",
        "to": "Ready for Testing",
        "assign": SALMA_ACCOUNT,
        "switch": "FLOW_APPROVE",
    },
}
# BASITA-151: the review guard. With FLOW_REVIEW_GUARD on, submit needs Architecture review green on the head of every
# open PR that mentions the issue; approve always does, and needs at least one such PR. A PR whose review isn't required
# is green, so submit works as before for it, and an issue with no open PR (work that isn't code) submits as before.
REVIEW_GUARDED = ("submit", "approve")
# The switches, in basita-ops jira-agents.conf; missing means off. The file is read, never run: only NAME="value" or
# NAME=value lines count, and the last one wins. ATL_AGENTS_CONF points to another file (tests).
FLOW_SWITCHES = ("FLOW_REVIEW_GUARD", "FLOW_APPROVE")
AGENTS_CONF = Path.home() / "Code" / "basita-ops" / "jira-agents.conf"


# jira-done (BASITA-178; Ahmed, 03/11/2026, operating model 4 "Moving one's own issues"): a lead moves its own issue
# from In Progress to Done when the work has no PR. DFP keeps jira-flow and the merge (4.1 item 7), so it is not here.
DONE_PROJECTS = ("BASITA", "ELB")
# An agent with no Jira seat -> the agent whose user it works through. Its issues are assigned to that user and carry
# the label agent-<name> (jira-agents.conf FOCAL_AGENT and ROUTED_AGENTS in basita-ops).
ROUTED_THROUGH = {"maya": "sherif"}
# Labels that hold an issue for Ahmed: jira-done leaves such an issue as it is.
WAITS_FOR_AHMED = ("needs-ahmed",)
# jira-wontdo (BASITA-203; Sherif's scope, 09/11/2026): a lead cancels an issue as Won't Do, with a reason. The BASITA
# and ELB workflows had no Won't Do status on 09/11/2026 (To Do, In Progress, Done only), so until Ahmed adds one
# the issue goes to Done and carries WONT_DO_LABEL, which tells a cancelled issue from a delivered one. The status is
# looked up by name at each call, so adding it to the workflow needs no change here.
WONT_DO_STATUS = "Won't Do"
WONT_DO_LABEL = "wont-do"
# Who may pass --ahmed to jira-wontdo. `atl` cannot verify Ahmed's words, so this power is not every agent's: it is
# the named agents' (Sherif, whose scope this is; add Talia when she has a Jira seat). Any other lead cancels only
# its own issue, with --why (Yousef's review of PR 53, 09/11/2026).
WONT_DO_ON_AHMEDS_WORD = ("sherif",)


def _status_key(name: str) -> str:
    """A status name as letters only, so Won't Do, Wont Do and Won’t Do are one name."""
    return re.sub(r"[^a-z]", "", name.lower())


# The repositories whose PRs name Basita's issues: the same list as REPOS in basita-ops jira-attention.sh.
PR_REPOS = (
    "basitasolutions/basita-ops",
    "basitasolutions/elbayoumy-farm-nutrition-platform",
    "ahmedoraby-eng/engineering-playbook",
)
# PRs read per repository. A repository that returns this many may have more, so jira-done can't be sure and refuses.
PR_LIST_LIMIT = 1000


def mentions(pr: dict, key: str) -> bool:
    """Whether a PR mentions an issue, by the rule of basita-ops jira-attention.sh: the key in its title or branch,
    or after `Closes` or `Refs` in its description, outside HTML comments (the PR template's own instructions)."""
    named = re.compile(rf"\b{re.escape(key)}\b", re.IGNORECASE)
    if named.search(pr.get("title") or "") or named.search(pr.get("headRefName") or ""):
        return True
    body = re.sub(r"<!--.*?-->", "", pr.get("body") or "", flags=re.DOTALL)
    return bool(re.search(rf"\b(?:closes|refs):?\s+{re.escape(key)}\b", body, flags=re.IGNORECASE))


# The playbook's own agent-gh, found from this file and not from PATH: the PRs are read as basita-reader, never through
# the Mac's gh login, which is Ahmed's (nothing on GitHub goes through his account, reads included; plan 10.1, 10.3).
AGENT_GH = str(Path(__file__).resolve().parents[2] / "scripts" / "agent-gh")


def list_repo_prs(repo: str, run: Callable = subprocess.run) -> list[dict]:
    """Every PR of one repository, open, closed and merged, as gh gives them (number, title, headRefName, body, state,
    url), read with `agent-gh reader pr list`. Raises AtlError when the repository can't be read in full (agent-gh
    refusing the reader included), so a caller never takes "can't tell" for "none". There is no fallback to plain gh."""
    fields = "number,title,headRefName,body,state,url"
    command = [AGENT_GH, "reader", "pr", "list", "-R", repo, "--state", "all", "--limit", str(PR_LIST_LIMIT)]
    try:
        result = run([*command, "--json", fields], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        raise AtlError(f"could not read the pull requests of {repo}: {error}") from None
    if result.returncode != 0:
        detail = " ".join((result.stderr or "").split())[:200]
        reason = detail or f"agent-gh exited {result.returncode}"
        raise AtlError(f"could not read the pull requests of {repo}: {reason}")
    try:
        prs = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        raise AtlError(f"could not read the pull requests of {repo}: agent-gh did not answer with JSON") from None
    if not isinstance(prs, list):
        raise AtlError(f"could not read the pull requests of {repo}: agent-gh did not answer with a list")
    if len(prs) >= PR_LIST_LIMIT:
        raise AtlError(f"{repo} has {PR_LIST_LIMIT} or more pull requests, more than atl reads")
    return prs


def github_prs(key: str, run: Callable = subprocess.run, repos: tuple = PR_REPOS) -> list[dict]:
    """The PRs in PR_REPOS that mention `key`, as {repo, number, state, url}, read with `agent-gh reader pr list`.
    Raises AtlError when any repository can't be read in full (see list_repo_prs)."""
    found: list[dict] = []
    for repo in repos:
        found += [
            {"repo": repo, "number": pr.get("number"), "state": pr.get("state"), "url": pr.get("url")}
            for pr in list_repo_prs(repo, run)
            if mentions(pr, key)
        ]
    return found


def flow_settings(path: str | Path | None = None) -> dict:
    """FLOW_REVIEW_GUARD and FLOW_APPROVE, "off" or "on"; "off" when the file or the line is missing. Any other value is
    refused, so a typo never switches a guard off or a step on."""
    path = Path(path or os.environ.get("ATL_AGENTS_CONF") or AGENTS_CONF)
    settings = dict.fromkeys(FLOW_SWITCHES, "off")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return settings
    for line in lines:
        match = re.match(r'^(FLOW_[A-Z_]+)=(?:"([^"]*)"|([^"\s#]*))\s*(?:#.*)?$', line)
        if match and match.group(1) in settings:
            settings[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    for name, value in settings.items():
        if value not in ("off", "on"):
            raise AtlError(f"{name} in {path} is '{value}', not off or on: nothing moved")
    return settings


def _reader_api(run: Callable, path: str, jq: str) -> str:
    result = run([AGENT_GH, "reader", "api", path, "--jq", jq], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        detail = " ".join((result.stderr or "").split())[:200]
        raise AtlError(f"could not read {path.split('?')[0]} as basita-reader: {detail or result.returncode}")
    return result.stdout.strip()


def is_architecture_review(name: str) -> bool:
    """The check run's name: "Architecture review", or "<caller job> / Architecture review" from a reusable workflow."""
    return name == "Architecture review" or name.endswith(" / Architecture review")


def arch_review_state(repo: str, number: int, run: Callable = subprocess.run) -> dict:
    """Architecture review on a PR's head, from the check's machine-readable notice line
    ("arch-review mode=on class=… state=green|waiting|changes|error head=<sha> …"), read as basita-reader. The line, not
    the conclusion, decides: in report mode the check is always green. state is "missing" with no check on the head,
    "running" while it runs, and "unreadable" with no such line (Architecture review off on that repository)."""
    head = _reader_api(run, f"repos/{repo}/pulls/{number}", ".head.sha")
    runs = json.loads(
        _reader_api(
            run,
            f"repos/{repo}/commits/{head}/check-runs?filter=latest&per_page=100",
            "[.check_runs[] | {id, name, status}]",
        )
        or "[]"
    )
    found = [r for r in runs if is_architecture_review(r.get("name") or "")]
    if not found:
        return {"head": head, "state": "missing"}
    if found[0].get("status") != "completed":
        return {"head": head, "state": "running"}
    messages = json.loads(
        _reader_api(run, f"repos/{repo}/check-runs/{found[0]['id']}/annotations", "[.[] | .message]") or "[]"
    )
    line = next((m for m in messages if isinstance(m, str) and m.startswith("arch-review ")), "")
    facts = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
    return {"state": "unreadable", **facts, "head": head, "checked": facts.get("head", "")}


def review_guard(
    issue: str, action: str, prs: Callable[[str], list[dict]], review: Callable[[str, int], dict]
) -> list[str]:
    """Why submit or approve may not move the issue now; empty when it may. Every open PR that mentions the issue
    must have Architecture review green on its current head. approve also needs one such PR. A PR that can't be read
    refuses."""
    try:
        found = prs(issue)
    except AtlError as error:
        return [f"can't tell which PRs mention it ({error})"]
    open_prs = [p for p in found if p.get("state") == "OPEN"]
    if action == "approve" and not open_prs:
        return ["no open PR mentions it, so there is no review to approve"]
    problems = []
    for pr in open_prs:
        where = pr.get("url") or f"{pr.get('repo')}#{pr.get('number')}"
        try:
            state = review(pr["repo"], int(pr["number"]))
        except (AtlError, ValueError, KeyError, TypeError) as error:
            problems.append(f"{where}: Architecture review can't be read ({error})")
            continue
        if state.get("state") != "green":
            problems.append(f"{where}: Architecture review is {state.get('state')} on {str(state.get('head'))[:7]}")
        elif state.get("checked") != state.get("head"):
            problems.append(f"{where}: Architecture review ran on {str(state.get('checked'))[:7]}, not the head")
    return problems


def account_for(agent: str, via: str = "") -> str:
    """The Atlassian user an agent works through: its own, unless --via names another agent's."""
    account = (via or agent).lower()
    if account not in AGENTS:
        raise AtlError(f"'{account}' has no Atlassian user to work through")
    return account


def _ahmed_only(label: str) -> bool:
    return label.lower() in AHMED_ONLY_LABELS


def _check_label(label: str, action: str = "set") -> None:
    """An agent's label: one word, and never one only Ahmed sets."""
    if not label or re.search(r"\s", label) or _ahmed_only(label):
        raise AtlError(f"an agent does not {action} the label '{label}'")


def _check_issue(key: str) -> None:
    """An issue an agent edits or links: a key in one of the projects agents create in."""
    if not ISSUE_KEY.match(key):
        raise AtlError(f"'{key}' is not an issue key")
    if key.split("-")[0] not in CREATE_PROJECTS:
        raise AtlError(f"'{key}' is not in a project an agent edits (known: {', '.join(CREATE_PROJECTS)})")


def _names(values, what: str) -> list[str]:
    """Labels or Fix Version names from a list, each stripped. A name is one line of 1 to 255 characters."""
    names = [v.strip() for v in values]
    for name in names:
        if not name or "\n" in name or len(name) > 255:
            raise AtlError(f"'{name}' is not a {what}")
    return names


# Jira Cloud's four built-in link types (name, outward phrase, inward phrase). jira_link_typed maps a name or phrase
# to one of these without reading /rest/api/3/issueLinkType, which the agents' tokens often can't read; any other
# name still goes through that read.
BUILTIN_LINK_TYPES = (
    {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
    {"name": "Relates", "outward": "relates to", "inward": "relates to"},
    {"name": "Duplicate", "outward": "duplicates", "inward": "is duplicated by"},
    {"name": "Cloners", "outward": "clones", "inward": "is cloned by"},
)


def _pick_link_type(wanted: str, types) -> tuple[dict | None, bool]:
    """The link type a name or phrase means, and whether it is the inward phrase (which reverses the two issues)."""
    for t in types:
        if wanted in (t.get("name", "").lower(), t.get("outward", "").lower()):
            return t, False
        if wanted == t.get("inward", "").lower():
            return t, True
    return None, False


# Confluence: a label is one lowercase word without these characters, at most 255 long.
MAX_LABEL = 255
_BAD_LABEL_CHARS = re.compile(r"[\s&#()*,.?;:@\[\]^/<>\"'\\|!]")
# One attachment: the cap keeps a stray big file from being read into memory and uploaded.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MOVE_DEPTH_CAP = 50  # how many levels above the new parent a move walks looking for the page itself
# What confluence-attach may send (Yousef's review of #41): images and documents for the WoW pages, nothing else, so a
# session can't publish a Mac-local secret (a .pem, an .env, a token file) to a page in one command. Each type has a
# fixed content type rather than a guess. Add a type here, and say so in its PR, before attaching it.
ATTACH_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
}
# Folders in the home folder where keys, tokens and app data live; nothing under them is attached, whatever its name.
ATTACH_REFUSED_HOME = (".config", ".ssh", ".gnupg", ".aws", ".claude", "Library")


def attach_refusal(path: str, home: Path | None = None) -> str:
    """Why `path` may not be attached, or "" when it may. Only the path is looked at, never the contents, and every
    reason names the rule. The path is resolved, so a symlink is judged by what it points to, and both the name given
    and the resolved one must pass: no folder or file whose name starts with "." (a dotfile, ~/.ssh), nothing under
    the ATTACH_REFUSED_HOME folders, and an extension in ATTACH_TYPES (which rules out .pem, .key, .p12, .env, .json,
    .conf, .txt and no extension at all)."""
    home = Path(home or Path.home()).resolve()
    given = Path(os.path.abspath(Path(path).expanduser()))
    resolved = given.resolve()
    allowed = ", ".join(ATTACH_TYPES)
    for candidate in (given, resolved):
        dotted = next((part for part in candidate.parts if part.startswith(".")), "")
        if dotted:
            return f"refused: atl attaches no dotfile and nothing in a dot folder ('{dotted}' in {candidate})"
        try:
            inside = candidate.relative_to(home).parts
        except ValueError:
            inside = ()
        if inside and inside[0] in ATTACH_REFUSED_HOME:
            return f"refused: atl attaches nothing under ~/{inside[0]}, where keys, tokens and app data live"
        ext = candidate.suffix.lower().lstrip(".")
        if ext not in ATTACH_TYPES:
            what = f".{ext}" if ext else "no extension"
            return f"refused: atl attaches only {allowed} files ({candidate.name} has {what})"
    return ""


def _check_confluence_labels(labels) -> list[str]:
    """Confluence label names, checked before any call: lowercase, no spaces or punctuation Confluence refuses."""
    names = list(dict.fromkeys(str(x).strip() for x in labels))
    for name in names:
        if not name or len(name) > MAX_LABEL or _BAD_LABEL_CHARS.search(name) or name != name.lower():
            raise AtlError(
                f"'{name}' is not a Confluence label: use lowercase, no spaces or punctuation "
                f"(& # ( ) * , . ? ! ; : @ [ ] ^ / < > quotes), up to {MAX_LABEL} characters"
            )
    return names


def _multipart(fields: dict[str, str], filename: str, content_type: str, data: bytes) -> tuple[str, bytes]:
    """A multipart/form-data body with one file part named `file` and the given text fields, in pure stdlib.
    Returns (Content-Type header, body). The boundary is chosen so it does not occur in the file."""
    while True:
        boundary = "atl-" + uuid.uuid4().hex
        if boundary.encode() not in data:
            break
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


class AtlClient:
    def __init__(
        self,
        agent: str,
        cloud_id: str,
        token: str,
        domain: str = "basitasolutions.com",
        transport: Transport = urllib_transport,
        via: str = "",
    ):
        agent = agent.lower()
        if agent not in AGENTS:
            raise AtlError(f"unknown agent '{agent}' (known: {', '.join(AGENTS)})")
        account = account_for(agent, via)
        if not cloud_id:
            raise AtlError("no Atlassian cloud id: set ATL_CLOUD_ID or pass --cloud-id")
        self.agent = agent
        self.display = agent.capitalize()
        self.via = "" if account == agent else account.capitalize()
        self.cloud_id = cloud_id
        self._auth = "Basic " + base64.b64encode(f"{account}@{domain}:{token}".encode()).decode()
        self._transport = transport

    @property
    def signature(self) -> str:
        """Who did the work, as it appears on a Jira comment."""
        return f"{self.display} (Basita AI, via {self.via})" if self.via else f"{self.display} (Basita AI)"

    @property
    def reader(self) -> str:
        return f"{self.display} (via {self.via})" if self.via else self.display

    def __repr__(self) -> str:  # keep the token out of any accidental print
        return f"AtlClient(agent={self.agent!r}, cloud_id={self.cloud_id!r})"

    def _call(
        self,
        method: str,
        product: str,
        path: str,
        payload: dict | list | None = None,
        *,
        raw: tuple[str, bytes] | None = None,
        headers: dict | None = None,
    ) -> dict:
        """One call through the gateway. `payload` is sent as JSON; `raw` is a (Content-Type, body) pair sent as it is
        (an upload); `headers` adds to the standard ones."""
        url = f"{GATEWAY}/{product}/{self.cloud_id}{path}"
        sent = {"Authorization": self._auth, "Accept": "application/json", **(headers or {})}
        body = None
        if raw is not None:
            sent["Content-Type"], body = raw
        elif payload is not None:
            sent["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        status, text = self._transport(method, url, sent, body)
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

    def _create_type(self, project: str, issue_type: str, parent: str) -> str:
        """Jira's name for the type, if this agent may create it here."""
        if issue_type == "Epic":
            if project not in EPIC_CREATORS.get(self.agent, ()):
                raise AtlError(f"{self.display} does not create an Epic in {project}")
            if parent:
                raise AtlError("an Epic has no parent")
            return "Epic"
        if issue_type not in CREATE_TYPES:
            raise AtlError(f"an agent does not create a '{issue_type}' (known: Epic, {', '.join(CREATE_TYPES)})")
        if CREATE_TYPES[issue_type] == "Subtask" and not parent:
            raise AtlError("a Sub-task needs --parent")
        return CREATE_TYPES[issue_type]

    def create_assignee(self, project: str, issue_type: str, assignee: str) -> str:
        """Who a new issue goes to. A bug in DFP goes to Adam first, to validate. Only Salma sends a clear technical
        bug straight to an engineer (operating model 4.3 item 11)."""
        if project == "DFP" and issue_type == "Bug":
            name = assignee.lower()
            if self.agent == "salma" and name in BUG_ENGINEERS:
                return name
            if name and name != "adam":
                raise AtlError(
                    f"a DFP Bug goes to Adam to validate, not to '{assignee}'; "
                    f"only Salma sends a clear technical bug to {' or '.join(n.capitalize() for n in BUG_ENGINEERS)}"
                )
            return "adam"
        return assignee

    def _unreleased_versions(self, issue: str) -> list[dict]:
        """The unreleased Fix Versions of an issue, as ids to set on another."""
        versions = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=fixVersions")["fields"]["fixVersions"]
        return [{"id": v["id"]} for v in versions if not v.get("released")]

    def _assignee_account(self, assignee: str) -> str:
        if self.agent not in ASSIGNERS:
            raise AtlError(f"{self.display} does not assign on create (only {', '.join(ASSIGNERS)})")
        account = JIRA_ACCOUNTS.get(assignee.lower())
        if not account:
            raise AtlError(f"'{assignee}' has no Jira seat to assign to (known: {', '.join(JIRA_ACCOUNTS)})")
        return account

    def jira_create(
        self,
        project: str,
        issue_type: str,
        summary: str,
        description: str,
        parent: str = "",
        labels: tuple = (),
        assignee: str = "",
        link: str = "",
    ) -> str:
        """Create an issue and return its key. Unassigned unless `assignee` is given, so it starts no pickup. A DFP
        Bug is the exception: it goes to Adam in Refinement. A technical one Salma sends to an engineer goes to
        Ready for Development instead, with the Fix Version of the issue in `link`, because pickup only starts an
        engineer there and with a release.
        Needs write:jira-work."""
        if project not in CREATE_PROJECTS:
            raise AtlError(f"'{project}' is not a project an agent creates in (known: {', '.join(CREATE_PROJECTS)})")
        type_name = self._create_type(project, issue_type, parent)
        chosen = self.create_assignee(project, issue_type, assignee)
        if project == "DFP" and issue_type == "Bug":  # the rule's choice, not this agent's, so any agent may
            account = JIRA_ACCOUNTS[chosen]
        else:
            account = self._assignee_account(chosen) if chosen else None
        summary = summary.strip()
        if not summary or "\n" in summary or len(summary) > 255:
            raise AtlError("the summary must be one line of 1 to 255 characters")
        if parent and not (ISSUE_KEY.match(parent) and parent.startswith(f"{project}-")):
            raise AtlError(f"'{parent}' is not an issue in {project}")
        for label in labels:
            _check_label(label)
        fields = {
            "project": {"key": project},
            "issuetype": {"name": type_name},
            "summary": summary,
            "description": _adf(description),
            "assignee": {"accountId": account} if account else None,
        }
        if parent:
            fields["parent"] = {"key": parent}
        if labels:
            fields["labels"] = list(labels)
        direct = project == "DFP" and issue_type == "Bug" and chosen in BUG_ENGINEERS
        if direct:
            if not (ISSUE_KEY.match(link) and link.startswith("DFP-")):
                raise AtlError("a technical Bug sent to an engineer needs --link to the DFP issue it came from")
            versions = self._unreleased_versions(link)
            if not versions:
                raise AtlError(f"{link} has no unreleased Fix Version, so the engineer would never be started")
            fields["fixVersions"] = versions
        key = self._call("POST", "jira", "/rest/api/3/issue", {"fields": fields})["key"]
        if direct:
            try:
                moves = self._call("GET", "jira", f"/rest/api/3/issue/{key}/transitions").get("transitions", [])
                chosen_move = next((t for t in moves if t["to"]["name"].lower() == BUG_DIRECT_STATUS.lower()), None)
                if not chosen_move:
                    raise AtlError("no such transition is available")
                move = {"transition": {"id": chosen_move["id"]}}
                self._call("POST", "jira", f"/rest/api/3/issue/{key}/transitions", move)
            except AtlError as error:
                raise AtlError(
                    f"{key} was created and assigned to {chosen.capitalize()}, but not moved to "
                    f"{BUG_DIRECT_STATUS} or linked to {link}: {error}"
                ) from error
        return key

    def jira_link(self, issue: str, other: str) -> None:
        """Link two issues with `relates to`. Needs write:jira-work."""
        if not ISSUE_KEY.match(other):
            raise AtlError(f"'{other}' is not an issue key")
        link = {"type": {"name": "Relates"}, "outwardIssue": {"key": issue}, "inwardIssue": {"key": other}}
        self._call("POST", "jira", "/rest/api/3/issueLink", link)

    def jira_edit(
        self,
        issue: str,
        *,
        add_labels: tuple = (),
        remove_labels: tuple = (),
        set_labels: tuple | None = None,
        assignee: str = "",
        description: str = "",
        priority: str = "",
        add_fix_versions: tuple = (),
        remove_fix_versions: tuple = (),
        set_fix_versions: tuple | None = None,
        summary: str = "",
        parent: str = "",
        duedate: str = "",
    ) -> list[str]:
        """Edit an issue's fields in one request, as this agent's own Jira user, and return what changed.

        Labels and Fix Versions can be added, removed, or set as a whole list (not both at once). The labels only
        Ahmed sets (AHMED_ONLY_LABELS, any case) are refused in every form: added, removed, or set. A whole list may
        only keep the issue's Ahmed-only labels exactly as they are, case included. The issue is in BASITA, DFP or ELB
        (CREATE_PROJECTS). `assignee` is a name with a Jira seat, or `none`
        to unassign; only the agents that assign on create may. `duedate` is YYYY-MM-DD or `none`. Every value is
        checked before any call; setting a whole label list first reads the issue's labels. Never deletes anything.
        Needs read:jira-work and write:jira-work."""
        _check_issue(issue)
        fields: dict = {}
        update: dict = {}
        changes: list[str] = []
        if set_labels is not None and (add_labels or remove_labels):
            raise AtlError("set the whole label list, or add and remove labels, not both")
        for label in add_labels:
            _check_label(label, "add")
        for label in remove_labels:
            _check_label(label, "remove")
        if add_labels or remove_labels:
            update["labels"] = [{"add": label} for label in add_labels] + [{"remove": label} for label in remove_labels]
            changes.append("labels " + " ".join([f"+{x}" for x in add_labels] + [f"-{x}" for x in remove_labels]))
        if set_labels is not None:
            new = list(dict.fromkeys(set_labels))
            for label in new:
                if not label or re.search(r"\s", label):
                    raise AtlError(f"an agent does not set the label '{label}'")
        if set_fix_versions is not None and (add_fix_versions or remove_fix_versions):
            raise AtlError("set the whole Fix Version list, or add and remove versions, not both")
        if add_fix_versions or remove_fix_versions:
            adds = _names(add_fix_versions, "Fix Version")
            removes = _names(remove_fix_versions, "Fix Version")
            update["fixVersions"] = [{"add": {"name": v}} for v in adds] + [{"remove": {"name": v}} for v in removes]
            changes.append("fix versions " + " ".join([f"+{v}" for v in adds] + [f"-{v}" for v in removes]))
        if set_fix_versions is not None:
            names = list(dict.fromkeys(_names(set_fix_versions, "Fix Version")))
            fields["fixVersions"] = [{"name": v} for v in names]
            changes.append(f"fix versions = {', '.join(names) or '(none)'}")
        if assignee:
            if self.agent not in ASSIGNERS:
                raise AtlError(f"{self.display} does not assign issues (only {', '.join(ASSIGNERS)})")
            if assignee.lower() == "none":
                fields["assignee"] = None
                changes.append("unassigned")
            else:
                account = JIRA_ACCOUNTS.get(assignee.lower())
                if not account:
                    raise AtlError(f"'{assignee}' has no Jira seat to assign to (known: {', '.join(JIRA_ACCOUNTS)})")
                fields["assignee"] = {"accountId": account}
                changes.append(f"assigned to {assignee.capitalize()}")
        if description:
            if not description.strip():
                raise AtlError("the description is empty")
            fields["description"] = _adf(description)
            changes.append("description")
        if priority:
            chosen = next((p for p in PRIORITIES if p.lower() == priority.strip().lower()), None)
            if not chosen:
                raise AtlError(f"'{priority}' is not a priority (known: {', '.join(PRIORITIES)})")
            fields["priority"] = {"name": chosen}
            changes.append(f"priority {chosen}")
        if summary:
            summary = summary.strip()
            if not summary or "\n" in summary or len(summary) > 255:
                raise AtlError("the summary must be one line of 1 to 255 characters")
            fields["summary"] = summary
            changes.append("summary")
        if parent:
            project = issue.split("-")[0]
            if not (ISSUE_KEY.match(parent) and parent.startswith(f"{project}-")) or parent == issue:
                raise AtlError(f"'{parent}' is not another issue in {project}")
            fields["parent"] = {"key": parent}
            changes.append(f"parent {parent}")
        if duedate:
            if duedate.lower() == "none":
                fields["duedate"] = None
                changes.append("no due date")
            else:
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", duedate):
                        raise ValueError
                    datetime.date.fromisoformat(duedate)
                except ValueError:
                    raise AtlError(f"'{duedate}' is not a date (YYYY-MM-DD, or none)") from None
                fields["duedate"] = duedate
                changes.append(f"due {duedate}")
        if set_labels is None and not fields and not update:
            raise AtlError("nothing to change: name at least one field")
        if set_labels is not None:
            data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=labels")
            current = data["fields"].get("labels") or []
            # Exactly, case included: NEEDS-AHMED for needs-ahmed would replace the label Ahmed set.
            kept = sorted(x for x in new if _ahmed_only(x))
            had = sorted(x for x in current if _ahmed_only(x))
            if kept != had:
                raise AtlError(
                    f"the new label list would change the labels only Ahmed sets ({', '.join(had) or 'none'} now); "
                    "keep them exactly as they are, or use --add-label and --remove-label for the others"
                )
            fields["labels"] = new
            changes.append(f"labels = {', '.join(new) or '(none)'}")
        body: dict = {}
        if fields:
            body["fields"] = fields
        if update:
            body["update"] = update
        self._call("PUT", "jira", f"/rest/api/3/issue/{issue}", body)
        return changes

    def jira_link_typed(self, issue: str, other: str, link_type: str = "Relates") -> str:
        """Link two issues so that `issue <relation> other` reads as asked, and return the relation as Jira now
        shows it on `issue`. `link_type` is a link type's name (Blocks), its outward phrase (blocks) or its inward
        phrase (is blocked by), without case; an inward phrase reverses the two issues. Both issues are in BASITA, DFP
        or ELB. The issue's links are read before and after the call, so the result shows what this call recorded
        (or the same link, already there), and an error says so if it reads the other way. Jira's four built-in types
        (Relates, Blocks, Duplicate, Cloners; BUILTIN_LINK_TYPES) are mapped here, without reading the site's link-type
        list; any other name reads it.
        Needs read:jira-work and write:jira-work."""
        for key in (issue, other):
            _check_issue(key)
        if issue == other:
            raise AtlError("an issue is not linked to itself")
        wanted = link_type.strip().lower()
        chosen, reverse = _pick_link_type(wanted, BUILTIN_LINK_TYPES)
        if not chosen:  # not one of Jira's four built-in types: read the site's list
            types = self._call("GET", "jira", "/rest/api/3/issueLinkType").get("issueLinkTypes", [])
            chosen, reverse = _pick_link_type(wanted, types)
        if not chosen:
            names = ", ".join(f"{t['name']} ({t['outward']} / {t['inward']})" for t in types)
            raise AtlError(f"'{link_type}' is not a link type (known: {names})")
        subject, target = (other, issue) if reverse else (issue, other)
        # Jira reads a link as "inwardIssue <outward phrase> outwardIssue": {"inwardIssue": A, "outwardIssue": B}
        # with Blocks shows "A blocks B". The read-back below checks it.
        link = {"type": {"name": chosen["name"]}, "inwardIssue": {"key": subject}, "outwardIssue": {"key": target}}
        # The links already there are read first, so a link recorded backwards is caught even when the right one
        # existed before this call.
        before = self._links_to(issue, other, chosen["name"])
        self._call("POST", "jira", "/rest/api/3/issueLink", link)
        expected = chosen["inward"] if reverse else chosen["outward"]
        after = self._links_to(issue, other, chosen["name"])
        seen = [relation for relation in after if relation not in before]
        if expected in seen or (not seen and expected in before):
            return expected
        if seen:
            raise AtlError(
                f"Jira recorded {issue} {seen[0]} {other}, not {issue} {expected} {other}: "
                "tell Talia, who has it corrected by hand (atl never deletes a link)"
            )
        raise AtlError(f"the link was sent, but {issue} does not show a {chosen['name']} link to {other} yet")

    def _links_to(self, issue: str, other: str, type_name: str) -> list[str]:
        """How `issue` shows its links of `type_name` to `other`: the phrases, e.g. ["blocks"]."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=issuelinks")["fields"]
        relations = []
        for found in data.get("issuelinks", []):
            if found.get("type", {}).get("name") != type_name:
                continue
            outward = "outwardIssue" in found
            if (found["outwardIssue"] if outward else found["inwardIssue"])["key"] != other:
                continue
            relations.append(found["type"]["outward" if outward else "inward"])
        return relations

    def jira_status(self, issue: str) -> dict:
        """The issue's status: {"name": ..., "category": "new" | "indeterminate" | "done"}.
        Needs read:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status")
        status = data["fields"]["status"]
        return {"name": status["name"], "category": status["statusCategory"]["key"]}

    def jira_transitions(self, issue: str) -> list[dict]:
        """The moves Jira offers on the issue now, as {"to": status name, "global": bool}. Needs read:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions")
        return [{"to": t["to"]["name"], "global": bool(t.get("isGlobal"))} for t in data.get("transitions", [])]

    def jira_get(self, issue: str, comments: str = "all", history: bool = False) -> dict:
        """An issue's fields, comments and linked issues, read as this agent. Only makes GET calls.
        `assigned_to_reader` comes from a currentUser() search, which shows whose token this is, since these
        tokens can't call /myself. `comments` is "all" or a number N for the last N (then `comments_total` says how
        many there are); `history` adds the changelog, one entry per change (BASITA-219). Needs read:jira-work."""
        if not ISSUE_KEY.match(issue):
            raise AtlError(f"'{issue}' is not an issue key")
        keep = comment_limit(comments)
        fields = "summary,status,issuetype,labels,assignee,reporter,priority,parent,description,issuelinks,subtasks"
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields={fields}")["fields"]
        comments = [
            {
                "id": c.get("id"),
                "author": (c.get("author") or {}).get("displayName"),
                "created": c.get("created"),
                "text": _adf_text(c.get("body")),
            }
            for c in self._comments(issue)
        ]
        total = len(comments)
        if keep is not None:
            comments = comments[-keep:] if keep else []
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
        extra = {"comments_total": total} if total != len(comments) else {}
        if history:
            extra["history"] = snap.history_entries(self._changelog(issue))
        return {
            "key": issue,
            "read_by": self.reader,
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
            "subtasks": [
                {"key": s["key"], "summary": s["fields"]["summary"], "status": s["fields"]["status"]["name"]}
                for s in data.get("subtasks", [])
            ],
            "links": links,
            "comments": comments,
            **extra,
        }

    def jira_history(self, issue: str) -> list[str]:
        """The issue's changelog as lines, one per change: when, who, field, from, to. Only makes GET calls."""
        if not ISSUE_KEY.match(issue):
            raise AtlError(f"'{issue}' is not an issue key")
        return [snap.history_line(entry) for entry in snap.history_entries(self._changelog(issue))]

    def _comments(self, issue: str) -> list[dict]:
        """All of an issue's comments as Jira gives them, oldest first, reading every page."""
        comments: list[dict] = []
        while True:
            query = f"startAt={len(comments)}&maxResults=100"
            page = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/comment?{query}")
            found = page.get("comments", [])
            comments += found
            if not found or len(comments) >= page.get("total", 0):
                break
        return comments

    def _changelog(self, issue: str) -> list[dict]:
        """All of an issue's change records as Jira gives them (BASITA-219), reading every page."""
        histories: list[dict] = []
        while True:
            page = self._call(
                "GET", "jira", f"/rest/api/3/issue/{issue}/changelog?startAt={len(histories)}&maxResults=100"
            )
            found = page.get("values", [])
            histories += found
            if not found or page.get("isLast", True) or len(histories) >= page.get("total", 0):
                break
        return histories

    def jira_md(self, issue: str, out_dir: str | Path, run: Callable = subprocess.run) -> dict:
        """Write the issue as Markdown to <out_dir>/work/jira/<KEY>.md, a read copy for agents (BASITA-205, ADR-B001).

        One cheap read of `updated` first: when the file already records that value, nothing else is read or
        written. Otherwise the issue and all its comments are read, the file is replaced atomically, and the issue
        gets its one link comment if no comment holds `work/jira/<KEY>.md` yet. Refuses an out_dir inside the app
        repository (AC4). Returns {"path", "changed", "commented"}. Needs read:jira-work, and write:comment:jira for
        the link comment."""
        if not ISSUE_KEY.match(issue):
            raise AtlError(f"'{issue}' is not an issue key")
        root = Path(out_dir).expanduser().resolve()
        if not root.is_dir():
            raise AtlError(f"no folder {root} for the snapshot: set BASITA_OPS_DIR or pass --out")
        folder = root.joinpath(*SNAPSHOT_FOLDER)
        place = next(p for p in [folder, *folder.parents] if p.exists())
        refuse_app_repo(place, run)
        path = folder / f"{issue}.md"
        archive = folder / f"{issue}.full.md"
        updated = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=updated")["fields"].get("updated")
        if updated and all(p.is_file() and _snapshot_updated(p) == updated for p in (path, archive)):
            return {"path": str(path), "changed": False, "commented": False}
        text, full, comments = self._snapshot(issue)
        folder.mkdir(parents=True, exist_ok=True)
        _write_atomically(archive, full)  # first, so a core file at this `updated` always has its archive
        _write_atomically(path, text)
        marker = snapshot_marker(issue)
        if any(marker in _adf_text(c.get("body")) for c in comments):
            return {"path": str(path), "changed": True, "commented": False}
        try:
            self.jira_comment(issue, f"{snapshot_comment(issue)}\n\n— {self.signature}")
        except AtlError as error:
            retry = f"tried again when {issue} changes"
            raise LinkCommentError(f"wrote {path}, but not the link comment ({retry}): {error}", str(path)) from None
        return {"path": str(path), "changed": True, "commented": True}

    def _snapshot(self, issue: str) -> tuple[str, str, list[dict]]:
        """The core file's text, the archive's text, read now, and the issue's comments as Jira gave them."""
        fields = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields={SNAPSHOT_FIELDS}")["fields"]
        comments = self._comments(issue)
        histories = self._changelog(issue)
        fetched = datetime.datetime.now(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        core, full = _snapshot_files(issue, fields, comments, histories, fetched)
        return core, full, comments

    def jira_md_text(self, issue: str) -> str:
        """The same Markdown as jira_md, read now and kept in memory: no file, no link comment. Only makes GET
        calls. For jira-get where there is no basita-ops folder (a cloud routine)."""
        if not ISSUE_KEY.match(issue):
            raise AtlError(f"'{issue}' is not an issue key")
        return self._snapshot(issue)[0]

    def jira_jql(self, jql: str, limit: int = 100) -> dict:
        """The issues a JQL query matches, as this agent, up to `limit`. Needs read:jira-work."""
        fields = "summary,status,issuetype,assignee,priority,parent,labels,fixVersions"
        issues: list[dict] = []
        token = ""
        while len(issues) < limit:
            query = f"jql={quote(jql)}&fields={fields}&maxResults={min(50, limit - len(issues))}"
            page = self._call(
                "GET", "jira", f"/rest/api/3/search/jql?{query}" + (f"&nextPageToken={token}" if token else "")
            )
            for issue in page.get("issues", []):
                f = issue["fields"]
                issues.append(
                    {
                        "key": issue["key"],
                        "summary": f.get("summary"),
                        "status": f["status"]["name"],
                        "type": f["issuetype"]["name"],
                        "priority": (f.get("priority") or {}).get("name"),
                        "assignee": (f.get("assignee") or {}).get("displayName"),
                        "parent": (f.get("parent") or {}).get("key"),
                        "labels": f.get("labels", []),
                        "fix_versions": [v.get("name") for v in f.get("fixVersions") or [] if v.get("name")],
                    }
                )
            token = page.get("nextPageToken", "")
            if not token or not page.get("issues"):
                break
        return {"read_by": self.reader, "jql": jql, "count": len(issues), "issues": issues}

    def jira_close(self, issue: str, to: str = "") -> str:
        """Move the issue to a status in the Done category and return that status's name. With `to`, only to
        the status of that name. Needs read:jira-work and write:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions")
        options = [
            t
            for t in data.get("transitions", [])
            if t["to"]["statusCategory"]["key"] == "done" and (not to or t["to"]["name"].lower() == to.lower())
        ]
        if not options:
            target = f"'{to}'" if to else "a Done status"
            raise AtlError(f"{issue}: no transition to {target} is available from its current status")
        chosen = next((t for t in options if t["to"]["name"] == "Done"), options[0])
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        return chosen["to"]["name"]

    def jira_start(self, issue: str) -> str:
        """Pick up an issue: To Do -> In Progress, add the `agent-picked` label, return the new status. An unassigned
        issue is assigned to the agent who starts it (BASITA-216 follow-up: Ahmed saw In Progress issues with no
        assignee, 08/11/2026); a routed agent's issue goes to its focal user. An assigned issue keeps its assignee.

        An agent's own start (Ahmed, 29/10/2026). Done comes from a merged PR with `Closes <KEY>`, or from
        jira-done for work with no open PR (BASITA-178). Refuses an issue that is not in
        a To Do status or was already picked, so a second pickup can't happen.
        Needs read:jira-work and write:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,labels,assignee")
        fields = data["fields"]
        status = fields["status"]["name"]
        unassigned = "assignee" in fields and not fields["assignee"]
        in_testing_queue = status.lower() == TESTING_QUEUE
        # In a `Ready for ...` queue the label comes from the previous agent: this is a handoff, not a second pickup.
        if PICKED_LABEL in fields.get("labels", []) and not status.lower().startswith("ready for"):
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
        # Move first: a failed move then leaves nothing behind, and the label is only ever on a started issue.
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        moved_to = chosen["to"]["name"]
        try:
            self._call("PUT", "jira", f"/rest/api/3/issue/{issue}", {"update": {"labels": [{"add": PICKED_LABEL}]}})
        except AtlError as e:
            raise AtlError(f"{issue}: moved to '{moved_to}', but the '{PICKED_LABEL}' label failed: {e}") from e
        account = JIRA_ACCOUNTS.get(ROUTED_THROUGH.get(self.agent, self.agent))
        if unassigned and account:
            try:
                self._call("PUT", "jira", f"/rest/api/3/issue/{issue}/assignee", {"accountId": account})
            except AtlError as e:
                raise AtlError(f"{issue}: moved to '{moved_to}' and labelled, but assigning it failed: {e}") from e
        return moved_to

    def _done_owner(self) -> tuple[str, str]:
        """For jira-done: the account the issue must be assigned to, and the agent label it must carry ("" for none).
        An agent with a Jira seat closes its own issues as itself; Maya closes hers through Sherif's user (2.4)."""
        routed = ROUTED_THROUGH.get(self.agent)
        if routed:
            if self.via.lower() != routed:
                raise AtlError(f"{self.display} has no Jira seat: run it as atl {self.agent} --via {routed} jira-done")
            return JIRA_ACCOUNTS[routed], f"agent-{self.agent}"
        if self.via:
            raise AtlError(f"jira-done closes {self.display}'s own issue as {self.display}'s own user, without --via")
        if self.agent not in JIRA_ACCOUNTS:
            raise AtlError(f"{self.display} has no Jira user, so no issue of {self.display}'s to close")
        return JIRA_ACCOUNTS[self.agent], ""

    def _require_own(self, issue: str, fields: dict, account: str, agent_label: str, command: str) -> list[str]:
        """The issue must be this agent's own (see _done_owner): assigned to `account`, and carrying `agent_label` when
        the agent is routed, and no routed agent's label otherwise. Returns the labels. Shared by jira-done and
        jira-wontdo, so a lead closes only its own work either way."""
        assignee = fields.get("assignee") or {}
        if assignee.get("accountId") != account:
            who = assignee.get("displayName") or "no one"
            raise AtlError(
                f"{issue}: assigned to {who}, not {self.reader}; {command} closes only the agent's own issue"
            )
        labels = fields.get("labels") or []
        routed = [f"agent-{name}" for name in ROUTED_THROUGH]
        if agent_label and agent_label not in labels:
            raise AtlError(f"{issue}: no '{agent_label}' label, so it is {self.via}'s own issue, not {self.display}'s")
        if not agent_label and any(label in routed for label in labels):
            mine = next(label for label in labels if label in routed)
            name = mine.removeprefix("agent-")
            raise AtlError(
                f"{issue}: labelled '{mine}', so it is {name.capitalize()}'s: "
                f"atl {name} --via {ROUTED_THROUGH[name]} {command}"
            )
        return labels

    def _require_token_user(self, issue: str) -> None:
        jql = quote(f"key = {issue} AND assignee = currentUser()")
        if not self._call("GET", "jira", f"/rest/api/3/search/jql?jql={jql}&fields=key").get("issues"):
            raise AtlError(f"{issue}: not assigned to the user whose token this is ({self.reader})")

    def _merged_prs(self, issue: str, prs: Callable[[str], list[dict]], open_hint: str) -> list[dict]:
        """The merged PRs that mention the issue, refusing (fail closed) when any is open, in an unknown state, or when
        the PRs can't be read. Closed-unmerged PRs are abandoned work and don't count, as in jira-attention.sh."""
        try:
            found = prs(issue)
        except AtlError as error:
            raise AtlError(
                f"{issue}: not closed, because atl can't tell whether every PR that mentions it is merged ({error})"
            ) from None
        unknown = [p for p in found if p.get("state") not in ("OPEN", "MERGED", "CLOSED")]
        if unknown:
            raise AtlError(f"{issue}: a PR that mentions it is in an unknown state: {unknown[0].get('url')}")
        still_open = [p for p in found if p["state"] == "OPEN"]
        if still_open:
            urls = ", ".join(p.get("url") or f"{p.get('repo')}#{p.get('number')}" for p in still_open)
            raise AtlError(f"{issue}: an open PR mentions it ({urls}); {open_hint}")
        return [p for p in found if p["state"] == "MERGED"]

    def jira_done(self, issue: str, why: str, prs: Callable[[str], list[dict]] = github_prs) -> tuple[str, list[dict]]:
        """A lead's own issue from In Progress to Done, for work with no open PR (BASITA-178; Ahmed, 03/11/2026).

        Refuses: a project other than BASITA or ELB (DFP keeps jira-flow and the merge); an Epic; an issue not
        assigned to this agent (Maya's: assigned to Sherif, with the label agent-maya, run with --via sherif); a
        status outside the In Progress category; an issue held for Ahmed (needs-ahmed); any open PR that mentions it,
        since PR work closes through `Closes` on merge; and PRs it can't read (fail closed). An issue with no PR, or
        whose PRs are all merged, may close. Closed-unmerged PRs are abandoned work and don't count, as in
        jira-attention.sh. Every check comes before the first write. Then it posts `why` as a signed comment, and only
        then moves the issue, picking the transition by the Done status category: a Done issue never lacks its
        comment. It changes no label and deletes nothing. Returns the new status and the merged PRs.
        Needs read:jira-work, write:jira-work and write:comment:jira."""
        if not ISSUE_KEY.match(issue):
            raise AtlError(f"'{issue}' is not an issue key")
        project = issue.split("-")[0]
        if project == "DFP":
            raise AtlError(f"{issue}: DFP issues move through jira-flow and close on the merge, not with jira-done")
        if project not in DONE_PROJECTS:
            raise AtlError(f"{issue}: jira-done is for {' and '.join(DONE_PROJECTS)} issues only")
        why = why.strip()
        if not why:
            raise AtlError("say what was delivered, with --why")
        account, agent_label = self._done_owner()
        fields = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,assignee,labels,issuetype")[
            "fields"
        ]
        if (fields.get("issuetype") or {}).get("name") == "Epic":
            raise AtlError(f"{issue}: an Epic is never worked as a ticket, jira-done does not close it")
        labels = self._require_own(issue, fields, account, agent_label, "jira-done")
        held = [label for label in labels if label.lower() in WAITS_FOR_AHMED]
        if held:
            raise AtlError(f"{issue}: labelled '{held[0]}', it waits for Ahmed; jira-done leaves it as it is")
        status = fields["status"]
        if status["statusCategory"]["key"] == "done":
            raise AtlError(f"{issue}: already {status['name']}")
        if status["statusCategory"]["key"] != "indeterminate":
            raise AtlError(f"{issue}: status is '{status['name']}', jira-done only closes an issue In Progress")
        self._require_token_user(issue)
        merged = self._merged_prs(issue, prs, f"PR work closes through 'Closes {issue}' on its merge")
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        options = [t for t in moves if t["to"]["statusCategory"]["key"] == "done"]
        if not options:
            raise AtlError(f"{issue}: no transition to a Done status is available from '{status['name']}'")
        chosen = next((t for t in options if t["to"]["name"] == "Done"), options[0])
        if merged:
            listed = "\n".join(p.get("url") or f"{p.get('repo')}#{p.get('number')}" for p in merged)
            trail = f"Every pull request that mentions {issue} is merged:\n{listed}"
        else:
            trail = f"No pull request mentions {issue}."
        comment = f"Done: {why}\n\n{trail}\n\n— {self.signature}"
        self.jira_comment(issue, comment)
        try:
            self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        except AtlError as error:
            raise AtlError(
                f"{issue}: the comment is posted, but the move to '{chosen['to']['name']}' failed: {error}"
            ) from error
        return chosen["to"]["name"], merged

    def jira_wontdo(
        self, issue: str, why: str, ahmed: str = "", prs: Callable[[str], list[dict]] = github_prs
    ) -> tuple[str, bool, list[dict]]:
        """A lead cancels a BASITA or ELB issue as Won't Do, with the reason (BASITA-203). Two cases:

        - the lead's own issue (no `ahmed`): the same ownership rules as jira-done (assigned to this agent; Maya's
          through Sherif; not held by needs-ahmed; the token's user is the assignee);
        - an issue Ahmed cancelled (`ahmed` = his words, kept verbatim in the comment): any assignee, because the
          cancellation is his and the signed comment is the trail. Only the agents in WONT_DO_ON_AHMEDS_WORD may use
          it (`atl` can't verify his words); any other agent is refused before any call, and the comment names the
          agent that ran it next to his words.

        Refuses, before any write: DFP (it keeps jira-flow and the merge) and other projects; an Epic; an issue already
        Done; an open PR that mentions it (close or merge the PR first); PRs it can't read (fail closed). From any
        status that is not Done, so a To Do or In Progress issue may be cancelled. Then it posts a signed comment
        (the reason, Ahmed's words, the merged PRs), moves the issue to the status "Won't Do" when its workflow has
        one, and otherwise to Done with the label `wont-do` added after the move (WONT_DO_LABEL). It removes no label
        (agent-hold stays) and deletes nothing. Returns the new status, whether the label was added, and the merged PRs.
        Needs read:jira-work, write:jira-work and write:comment:jira."""
        if not ISSUE_KEY.match(issue):
            raise AtlError(f"'{issue}' is not an issue key")
        project = issue.split("-")[0]
        if project == "DFP":
            raise AtlError(f"{issue}: DFP issues move through jira-flow and close on the merge, not with jira-wontdo")
        if project not in DONE_PROJECTS:
            raise AtlError(f"{issue}: jira-wontdo is for {' and '.join(DONE_PROJECTS)} issues only")
        why = why.strip()
        if not why:
            raise AtlError("say why it is cancelled, with --why")
        ahmed = ahmed.strip()
        if ahmed and self.agent not in WONT_DO_ON_AHMEDS_WORD:
            raise AtlError(
                f"{issue}: only {', '.join(WONT_DO_ON_AHMEDS_WORD)} cancel on Ahmed's word (--ahmed); "
                f"{self.display} cancels its own issue with --why alone"
            )
        account, agent_label = ("", "") if ahmed else self._done_owner()
        fields = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,assignee,labels,issuetype")[
            "fields"
        ]
        if (fields.get("issuetype") or {}).get("name") == "Epic":
            raise AtlError(f"{issue}: an Epic is never worked as a ticket, jira-wontdo does not cancel it")
        status = fields["status"]
        if status["statusCategory"]["key"] == "done":
            raise AtlError(f"{issue}: already {status['name']}")
        if not ahmed:
            labels = self._require_own(issue, fields, account, agent_label, "jira-wontdo")
            held = [label for label in labels if label.lower() in WAITS_FOR_AHMED]
            if held:
                raise AtlError(
                    f"{issue}: labelled '{held[0]}', it waits for Ahmed; cancel it with his words (--ahmed) or leave it"
                )
            self._require_token_user(issue)
        merged = self._merged_prs(issue, prs, "close or merge that PR first, then cancel the issue")
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        wanted = _status_key(WONT_DO_STATUS)
        chosen = next((t for t in moves if _status_key(t["to"]["name"]) == wanted), None)
        labelled = chosen is None
        if chosen is None:
            options = [t for t in moves if t["to"]["statusCategory"]["key"] == "done"]
            if not options:
                raise AtlError(
                    f"{issue}: no transition to {WONT_DO_STATUS} or to a Done status from '{status['name']}'"
                )
            chosen = next((t for t in options if t["to"]["name"] == "Done"), options[0])
        lines = [f"{WONT_DO_STATUS}: {why}"]
        if ahmed:
            lines.append(f"Cancelled by Ahmed, in his words: «{ahmed}»")
            lines.append(f"Run by {self.display} on Ahmed's word.")
        if merged:
            listed = "\n".join(p.get("url") or f"{p.get('repo')}#{p.get('number')}" for p in merged)
            lines.append(f"Pull requests that mention {issue}, all merged:\n{listed}")
        else:
            lines.append(f"No pull request mentions {issue}.")
        if labelled:
            lines.append(
                f"This workflow has no '{WONT_DO_STATUS}' status, so the issue is closed as {chosen['to']['name']} and "
                f"labelled '{WONT_DO_LABEL}': it was cancelled, not delivered."
            )
        self.jira_comment(issue, "\n\n".join(lines) + f"\n\n— {self.signature}")
        try:
            self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
        except AtlError as error:
            raise AtlError(
                f"{issue}: the comment is posted, but the move to '{chosen['to']['name']}' failed: {error}"
            ) from error
        moved_to = chosen["to"]["name"]
        if labelled:
            try:
                self._call(
                    "PUT", "jira", f"/rest/api/3/issue/{issue}", {"update": {"labels": [{"add": WONT_DO_LABEL}]}}
                )
            except AtlError as error:
                raise AtlError(
                    f"{issue}: moved to '{moved_to}', but the '{WONT_DO_LABEL}' label failed: {error}; "
                    "add it with jira-edit"
                ) from error
        return moved_to, labelled, merged

    def jira_ready(self, issue: str, assignee: str = "") -> str:
        """Refinement -> Ready (or Ready for Development in DFP), the one transition an agent makes on a
        refined issue (Ahmed, 30/10/2026).

        Refuses an Epic (never worked as a ticket) and any issue that is not in Refinement, and only ever
        takes a transition that lands on one of READY_STATUSES. In DFP it also names who gets the issue and needs
        an unreleased Fix Version, because pickup starts no one without them (Ahmed, 01/11/2026). It assigns before it
        moves, so running it again after a failure finishes the step. Needs read:jira-work and write:jira-work."""
        data = self._call("GET", "jira", f"/rest/api/3/issue/{issue}?fields=status,issuetype,fixVersions")
        fields = data["fields"]
        if fields["issuetype"]["name"] == "Epic":
            raise AtlError(f"{issue}: an Epic is never worked as a ticket")
        if fields["status"]["name"].lower() != "refinement":
            current = fields["status"]["name"]
            raise AtlError(f"{issue}: status is '{current}', an agent only readies issues in Refinement")
        account = ""
        if issue.startswith("DFP-"):
            if not assignee:
                raise AtlError(f"{issue}: name who gets it, with --assignee, or it waits on {self.display}")
            if assignee.lower() in READY_NOT_FOR:
                raise AtlError(f"{issue}: pickup never starts {assignee.capitalize()} on Ready for Development")
            if not [v for v in fields.get("fixVersions") or [] if not v.get("released")]:
                raise AtlError(
                    f"{issue}: no unreleased Fix Version, so no one would ever be started on it. It stays in "
                    "Refinement. Say so in a comment with @ to whoever owns the release: they set the version and "
                    f"assign it to {self.display} again."
                )
        if assignee:
            account = self._assignee_account(assignee)
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        chosen = next((t for t in moves if t["to"]["name"].lower() in READY_STATUSES), None)
        if not chosen:
            raise AtlError(f"{issue}: no transition to Ready or Ready for Development is available from Refinement")
        if account:
            self._call("PUT", "jira", f"/rest/api/3/issue/{issue}/assignee", {"accountId": account})
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

    def _last_engineer(self, issue: str) -> str:
        """The account of whoever last held the issue among SUBMITTER_ACCOUNTS (an engineer, or a lead on DFP work that
        isn't code), from its assignee history."""
        found = ""
        start = 0
        while True:
            page = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/changelog?startAt={start}&maxResults=100")
            entries = page.get("values", [])
            for entry in entries:  # oldest first
                for item in entry.get("items", []):
                    if item.get("field") == "assignee":
                        for account in (item.get("from"), item.get("to")):
                            if account in SUBMITTER_ACCOUNTS:
                                found = account
            start += len(entries)
            if not entries or page.get("isLast", True):
                break
        if not found:
            raise AtlError(f"{issue}: no one who works DFP issues is in its assignee history, so no one to go back to")
        return found

    def jira_flow(
        self,
        issue: str,
        action: str,
        prs: Callable[[str], list[dict]] = github_prs,
        review: Callable[[str, int], dict] = arch_review_state,
        settings: dict | None = None,
    ) -> str:
        """One step of DFP's flow, from FLOW: checks the agent and the status, assigns the next person, moves the
        issue and returns the new status. It assigns first, so running it again after a failure finishes the step.
        A row with a `switch` (approve) runs only when that switch is on; with FLOW_REVIEW_GUARD on, submit and
        approve run review_guard first (BASITA-151). Every check comes before the first write.
        Needs read:jira-work and write:jira-work."""
        step = FLOW.get(action)
        if not step:
            raise AtlError(f"unknown action '{action}' (known: {', '.join(FLOW)})")
        if not issue.startswith("DFP-"):
            raise AtlError(f"{issue}: jira-flow is for DFP only")
        if self.agent not in step["agents"]:
            raise AtlError(f"{action} is for {' or '.join(a.capitalize() for a in step['agents'])}, not {self.display}")
        switch = step.get("switch")
        guarded = action in REVIEW_GUARDED
        if switch or guarded:
            settings = settings if settings is not None else flow_settings()
        if switch and settings.get(switch) != "on":
            raise AtlError(f"{action} is off ({switch} in basita-ops jira-agents.conf); Ahmed switches it on")
        status = self.jira_status(issue)["name"]
        if status.lower() != step["from"].lower():
            raise AtlError(f"{issue}: status is '{status}', {action} only leaves '{step['from']}'")
        # approve is the reviewed step, so its guard always runs; submit's only with FLOW_REVIEW_GUARD on.
        if guarded and (action == "approve" or settings.get("FLOW_REVIEW_GUARD") == "on"):
            problems = review_guard(issue, action, prs, review)
            if problems:
                raise AtlError(f"{issue}: {action} waits for the architect's review: " + "; ".join(problems))
        assignee = self._last_engineer(issue) if step["assign"] == LAST_ENGINEER else step["assign"]
        moves = self._call("GET", "jira", f"/rest/api/3/issue/{issue}/transitions").get("transitions", [])
        chosen = next((t for t in moves if t["to"]["name"].lower() == step["to"].lower()), None)
        if not chosen:
            raise AtlError(f"{issue}: no transition to '{step['to']}' is available from '{status}'")
        self._call("PUT", "jira", f"/rest/api/3/issue/{issue}/assignee", {"accountId": assignee})
        self._call("POST", "jira", f"/rest/api/3/issue/{issue}/transitions", {"transition": {"id": chosen["id"]}})
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

    def confluence_get(self, page: str, storage: bool = False) -> dict:
        """A page, by id or URL: its details, labels, body as plain text, comments, attachments and child pages.
        With `storage`, also `storage`: the body in Confluence storage format, for an exact edit (BASITA-191).
        Only GET calls. Needs read:page, read:space, read:comment, read:attachment and
        read:hierarchical-content (all :confluence)."""
        page_id = self._page_id(page)
        data = self._call(
            "GET", "confluence", f"/wiki/api/v2/pages/{page_id}?body-format=atlas_doc_format&include-labels=true"
        )
        space = self._call("GET", "confluence", f"/wiki/api/v2/spaces/{data['spaceId']}") if data.get("spaceId") else {}
        comments = []
        for kind in ("footer", "inline"):
            found = self._results(
                f"/wiki/api/v2/pages/{page_id}/{kind}-comments?body-format=atlas_doc_format&limit=100"
            )
            comments += [
                {"id": c.get("id"), "kind": kind, "created": _version_at(c), "text": _doc_text(c)} for c in found
            ]
        attachments = self._results(f"/wiki/api/v2/pages/{page_id}/attachments?limit=100")
        children = self._results(f"/wiki/api/v2/pages/{page_id}/direct-children?limit=100")
        links = data.get("_links") or {}
        result = {
            "id": data.get("id"),
            "read_by": self.reader,
            "title": data.get("title"),
            "space": space.get("key"),
            "space_name": space.get("name"),
            "status": data.get("status"),
            "version": (data.get("version") or {}).get("number"),
            "updated": _version_at(data),
            "url": (links.get("base") or "") + (links.get("webui") or ""),
            "labels": [label.get("name") for label in (data.get("labels") or {}).get("results", [])],
            "parent": data.get("parentId"),
            "children": [{"id": c.get("id"), "title": c.get("title"), "type": c.get("type")} for c in children],
            "attachments": [{"title": a.get("title"), "size": a.get("fileSize")} for a in attachments],
            "body": _doc_text(data),
            "comments": comments,
        }
        if storage:
            raw = self._call("GET", "confluence", f"/wiki/api/v2/pages/{page_id}?body-format=storage")
            result["storage"] = ((raw.get("body") or {}).get("storage") or {}).get("value", "")
        return result

    def confluence_search(self, query: str, limit: int = 25) -> dict:
        """Pages matching a CQL query, or plain words searched in their text, as this agent.
        Needs search:confluence."""
        is_cql = any(mark in query for mark in ("=", "~", " AND ", " OR ", " order by "))
        words = query.replace('"', " ")
        cql = query if is_cql else f'type in (page, blogpost) AND text ~ "{words}"'
        data = self._call("GET", "confluence", f"/wiki/rest/api/search?cql={quote(cql)}&limit={min(limit, 100)}")
        base = (data.get("_links") or {}).get("base", "")
        results = [
            {
                "id": (item.get("content") or {}).get("id"),
                "type": (item.get("content") or {}).get("type"),
                "title": item.get("title") or (item.get("content") or {}).get("title"),
                "space": (item.get("resultGlobalContainer") or {}).get("title"),
                "updated": item.get("lastModified"),
                "excerpt": re.sub(r"@@@(end)?hl@@@", "", item.get("excerpt") or ""),
                "url": base + (item.get("url") or ""),
            }
            for item in data.get("results", [])
        ]
        return {"read_by": self.reader, "cql": cql, "count": len(results), "results": results}

    def confluence_create(
        self, space: str, title: str, text: str, parent: str = "", storage: bool = False, labels: tuple = ()
    ) -> dict:
        """Create a published page in a space, by its key, then add the labels (never removed once added). The labels
        are checked first; if adding them fails, the error names the page that now exists.
        Needs read:space and write:page (both :confluence), and for labels read:label and write:label."""
        if not _SPACE_KEY.match(space):
            raise AtlError(f"'{space}' is not a space key")
        names = _check_confluence_labels(labels)
        found = self._results(f"/wiki/api/v2/spaces?keys={quote(space)}")
        if not found:
            raise AtlError(f"no space with the key '{space}', or it is not visible to {self.display}")
        payload = {"spaceId": found[0]["id"], "status": "current", "title": title, "body": _page_body(text, storage)}
        if parent:
            payload["parentId"] = self._page_id(parent)
        result = self._call("POST", "confluence", "/wiki/api/v2/pages", payload)
        if names:
            self._add_labels(result.get("id"), names, "created")
            result["labels_added"] = names
        return result

    def confluence_edit(
        self,
        page: str,
        text: str = "",
        title: str = "",
        storage: bool = False,
        *,
        parent: str = "",
        labels: tuple = (),
    ) -> dict:
        """Replace a page's body, its title, or both; move it under another page (`parent`); add labels. Confluence
        keeps every earlier version, and the version note names the agent. A move keeps the page's id and stays in its
        space: the page and the new parent must be in the same space, the parent can't be the page or one of its own
        descendants, and all of that is read before anything is written. With no text, the current body is kept. The
        labels are only ever added. Labels alone change no version.
        Needs read:page and write:page (both :confluence), and for labels read:label and write:label."""
        names = _check_confluence_labels(labels)
        if not (text.strip() or title.strip() or parent.strip() or names):
            raise AtlError("nothing to change: give the new text, a new title, a new parent, a label, or several")
        page_id = self._page_id(page)
        parent_id = self._page_id(parent) if parent.strip() else ""
        if parent_id and parent_id == page_id:
            raise AtlError("a page can't be moved under itself")
        if not (text.strip() or title.strip() or parent_id):  # labels only: no new version of the page
            self._add_labels(page_id, names, "unchanged")
            return {"id": page_id, "labels_added": names}
        current = self._call("GET", "confluence", f"/wiki/api/v2/pages/{page_id}?body-format=storage")
        move = bool(parent_id) and parent_id != str(current.get("parentId") or "")
        if move:
            self._check_move(current, page_id, parent_id)
        if not (text.strip() or title.strip() or move):  # already under that parent
            if not names:
                raise AtlError(f"page {page_id} is already under page {parent_id}: nothing to change")
            self._add_labels(page_id, names, "unchanged")
            return {"id": page_id, "labels_added": names}
        if text.strip():
            body = _page_body(text, storage)
        else:
            body = {"representation": "storage", "value": current["body"]["storage"]["value"]}
        note = f"[{self.reader}] " + (
            text.strip()[:150] if text.strip() and not storage else f"edited: {title or 'body'}"
        )
        if move:
            note += f" (moved under page {parent_id})"
        payload = {
            "id": current["id"],
            "status": "current",
            "title": title.strip() or current["title"],
            "body": body,
            "version": {"number": current["version"]["number"] + 1, "message": note},
        }
        if move:
            payload["parentId"] = parent_id
        result = self._call("PUT", "confluence", f"/wiki/api/v2/pages/{page_id}", payload)
        if names:
            self._add_labels(page_id, names, "edited")
            result["labels_added"] = names
        return result

    def _check_move(self, current: dict, page_id: str, parent_id: str) -> None:
        """Refuses, with reads only, a move to another space or under the page's own descendants. A parent that
        isn't a page (a folder, say) isn't read by the v2 page endpoint and is refused as not found."""
        target = self._call("GET", "confluence", f"/wiki/api/v2/pages/{parent_id}")
        here, there = current.get("spaceId"), target.get("spaceId")
        if not here or not there:
            raise AtlError(f"can't tell which space page {page_id} or page {parent_id} is in: not moving it")
        if str(here) != str(there):
            raise AtlError(
                f"page {parent_id} is in another space (id {there}) than page {page_id} (id {here}): "
                "atl moves a page only within its space"
            )
        node = target
        for _ in range(MOVE_DEPTH_CAP):
            above = str(node.get("parentId") or "")
            if not above or node.get("parentType", "page") != "page":
                return
            if above == page_id:
                raise AtlError(f"page {parent_id} is under page {page_id}: a page can't be moved under its own child")
            node = self._call("GET", "confluence", f"/wiki/api/v2/pages/{above}")
        raise AtlError(f"page {parent_id} is more than {MOVE_DEPTH_CAP} levels deep: not moving {page_id} under it")

    def _add_labels(self, page_id, names: list[str], state: str) -> None:
        """Add labels to a page (POST only: a label already there stays, none is ever removed)."""
        body = [{"prefix": "global", "name": name} for name in names]
        try:
            self._call("POST", "confluence", f"/wiki/rest/api/content/{page_id}/label", body)
        except AtlError as error:
            raise AtlError(f"page {page_id} was {state}, but the labels were not added: {error}") from error

    def confluence_attach(self, page: str, path: str) -> dict:
        """Upload a file to a page, or add a new version when the page already has an attachment of that name
        (Confluence matches by file name). Nothing is deleted. attach_refusal decides first, from the path alone, so
        only an image or document outside the secret folders is ever read. The file is read in binary, must be a file
        (not a directory) of at most MAX_ATTACHMENT_BYTES, and goes up under its base name. Returns {"id", "name",
        "version", "ref"}, where `name` is the name to use in `<ri:attachment ri:filename="...">` and `ref` is a ready
        `<ac:image>` line. Needs read:content-details and write:attachment (both :confluence)."""
        page_id = self._page_id(page)
        refusal = attach_refusal(path)  # before the file is touched (Yousef's review of #41)
        if refusal:
            raise AtlError(refusal)
        source = Path(path).expanduser()
        if not source.exists():
            raise AtlError(f"no such file: {path}")
        if not source.is_file():
            raise AtlError(f"{path} is not a file")
        name = source.name
        if not name.strip() or re.search(r'["\r\n\\]', name):
            raise AtlError(f"the file name '{name}' can't be uploaded: no quotes, backslashes or line breaks")
        size = source.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise AtlError(
                f"{name} is {size / 1024 / 1024:.1f} MB; atl uploads at most {MAX_ATTACHMENT_BYTES // 1024 // 1024} MB"
            )
        data = source.read_bytes()
        content_type = ATTACH_TYPES[Path(name).suffix.lower().lstrip(".")]
        fields = {"minorEdit": "true", "comment": f"[{self.reader}] uploaded with atl"}
        raw = _multipart(fields, name, content_type, data)
        found = self._call(
            "PUT",
            "confluence",
            f"/wiki/rest/api/content/{page_id}/child/attachment",
            raw=raw,
            headers={"X-Atlassian-Token": "nocheck"},
        )
        item = (found.get("results") or [found])[0]
        used = item.get("title") or name
        return {
            "id": item.get("id"),
            "name": used,
            "version": (item.get("version") or {}).get("number"),
            "ref": f'<ac:image><ri:attachment ri:filename="{escape(used, quote=True)}"/></ac:image>',
        }

    def _page_id(self, page: str) -> str:
        match = _PAGE_ID.match(page.strip())
        if not match:
            raise AtlError(f"'{page}' is not a Confluence page id or URL")
        return match.group(1)

    def _results(self, path: str) -> list[dict]:
        """Every page of a Confluence v2 list, following its `next` links."""
        results: list[dict] = []
        while path:
            data = self._call("GET", "confluence", path)
            results += data.get("results", [])
            path = (data.get("_links") or {}).get("next") or ""
        return results

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
            "version": {"number": page["version"]["number"] + 1, "message": f"[{self.reader}] {text[:200]}"},
        }
        return self._call("PUT", "confluence", f"/wiki/api/v2/pages/{page_id}", payload)
