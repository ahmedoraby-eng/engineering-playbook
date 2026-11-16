"""The scope gate (BASITA-245; Yousef's ADR «a deterministic scope gate in the flow», 16/11/2026, Ahmed: G1 and G2).

G1: what is in and out of an issue's scope, from explicit Jira data only: the numbered acceptance criteria (AC1…ACn
in the description), the open linked issues (any type, both directions), the parent and the sub-tasks. Text search is
never used: the same issue gives the same answer every time.

G2: a send-back (Salma's fail, Adam's reject, the architect's changes) takes a findings file, not free text. Each
finding cites one of this issue's ACs, or names an open linked issue it belongs to. A finding with neither is
refused before anything is written; an out-of-scope finding is posted on its own ticket and never reaches the
developer of this one.

Pure functions here; the Jira calls are in client.py (issue_scope, send_back).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# An acceptance criterion is DEFINED at the start of a line: «AC1 …», «AC1: …», «- AC1 …», «1. AC1 …», «**AC1** …».
# A mention inside a sentence («as AC8 requires», «Relates to DFP-239 AC1/AC8») defines nothing: DFP-269's bug text
# names DFP-239's AC1 and AC8, which are not DFP-269's. Case-sensitive: the team writes «AC».
_AC = re.compile(r"^[ \t>]*(?:[-*•][ \t]*|\d{1,3}[.)][ \t]*)?(?:\*\*)?AC[ -]?(\d{1,3})(?![0-9A-Za-z])", re.MULTILINE)
_AC_ONLY = re.compile(r"\s*AC[ -]?(\d{1,3})\s*", re.IGNORECASE)  # a finding's «ac»: one AC, nothing else
MAX_FINDINGS = 50
MAX_WHAT = 2000
FINDING_KEYS = {"ac", "out_of_scope", "file", "what"}


class ScopeError(Exception):
    """A findings file or a scope that can't be used; nothing has been written."""


def parse_acs(text: str | None) -> list[str]:
    """The numbered acceptance criteria the text defines (one per line start), in order, as AC1, AC2, …"""
    seen: list[str] = []
    for match in _AC.finditer(text or ""):
        ac = f"AC{int(match.group(1))}"
        if ac not in seen:
            seen.append(ac)
    return seen


def _done(status: dict | None) -> bool:
    return ((status or {}).get("statusCategory") or {}).get("key") == "done"


def _brief_issue(issue: dict, relation: str = "") -> dict:
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    out = {
        "key": issue.get("key"),
        "summary": fields.get("summary") or "",
        "status": status.get("name") or "",
        "done": _done(status),
    }
    if relation:
        out["relation"] = relation
    return out


def scope_from_fields(key: str, fields: dict, description_text: str) -> dict:
    """G1's data for one issue, from its Jira fields (status, issuelinks, parent, subtasks) and its description as
    plain text. Open and closed are told apart by the status category (done or not), never by a status name."""
    links = []
    for link in fields.get("issuelinks") or []:
        outward = "outwardIssue" in link
        other = link.get("outwardIssue" if outward else "inwardIssue") or {}
        relation = ((link.get("type") or {}).get("outward" if outward else "inward")) or "relates to"
        if other.get("key"):
            links.append(_brief_issue(other, relation))
    parent = fields.get("parent")
    subtasks = [_brief_issue(s) for s in fields.get("subtasks") or [] if s.get("key")]
    return {
        "key": key,
        "acs": parse_acs(description_text),
        "open_links": [x for x in links if not x["done"]],
        "closed_links": [x for x in links if x["done"]],
        "parent": _brief_issue(parent) if parent and parent.get("key") else None,
        "open_subtasks": [x for x in subtasks if not x["done"]],
    }


def brief_lines(scope: dict, agent: str = "<you>") -> list[str]:
    """G1's text for a stage brief: the same data gives the same text, every run."""
    key = scope["key"]
    lines = ["Scope (BASITA-245, from Jira's own fields; the same every run):"]
    if scope["acs"]:
        lines.append(f"- In scope: {key}'s acceptance criteria {', '.join(scope['acs'])}, and nothing else.")
    else:
        lines.append(
            f"- In scope: {key} has no numbered acceptance criteria (AC1…). Nothing can be sent back to its developer "
            "as «changes needed» until Adam numbers them; ask Adam in a Jira comment."
        )
    if scope["open_links"]:
        lines.append("- Out of scope, has its own ticket:")
        for link in scope["open_links"]:
            lines.append(f"  - {link['key']} ({link['relation']}) «{link['summary']}», {link['status']}")
        lines.append(
            f"  A finding in that area goes there as a comment (atl {agent} jira-comment <that key> …), "
            f"not into {key}'s branch. To send work back, use the findings file (out_of_scope: <that key>)."
        )
    else:
        lines.append("- Out of scope, has its own ticket: none (no open linked issue).")
    if scope.get("parent"):
        p = scope["parent"]
        lines.append(f"- Parent: {p['key']} «{p['summary']}», {p['status']}.")
    if scope["open_subtasks"]:
        subtasks = ", ".join(f"{s['key']} «{s['summary']}»" for s in scope["open_subtasks"])
        lines.append(f"- Open sub-tasks: {subtasks}.")
    return lines


def load_findings(source: str | Path, text: str | None = None) -> list[dict]:
    """A findings file: a JSON list of {"ac": "AC3", "file": "…", "what": "…"} or
    {"out_of_scope": "DFP-273", "what": "…"}. Only the shape is checked here; check_findings checks them against
    the issue."""
    try:
        raw = text if text is not None else Path(source).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ScopeError(f"findings file {source}: not readable JSON ({error})") from error
    if not isinstance(data, list) or not data:
        raise ScopeError("findings file: a non-empty JSON list of findings")
    if len(data) > MAX_FINDINGS:
        raise ScopeError(f"findings file: at most {MAX_FINDINGS} findings")
    findings = []
    for n, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ScopeError(f"finding {n}: an object, not {type(item).__name__}")
        extra = set(item) - FINDING_KEYS
        if extra:
            raise ScopeError(f"finding {n}: unknown field(s) {', '.join(sorted(extra))}")
        what = item.get("what")
        if not isinstance(what, str) or not what.strip() or len(what) > MAX_WHAT:
            raise ScopeError(f"finding {n}: «what» must say what is wrong (1–{MAX_WHAT} characters)")
        has_ac, has_out = "ac" in item, "out_of_scope" in item
        if has_ac == has_out:
            raise ScopeError(f"finding {n}: exactly one of «ac» (this issue's AC) or «out_of_scope» (a linked issue)")
        for field in ("ac", "out_of_scope", "file"):
            if field in item and (not isinstance(item[field], str) or not item[field].strip()):
                raise ScopeError(f"finding {n}: «{field}» must be a non-empty string")
        findings.append({k: (v.strip() if isinstance(v, str) else v) for k, v in item.items()})
    return findings


def check_findings(scope: dict, findings: list[dict], agent: str = "<you>") -> tuple[list[dict], list[dict]]:
    """G2: (in scope, out of scope) or ScopeError. Every finding must cite one of this issue's ACs, or name one of its
    open linked issues. Nothing is guessed: a finding that fits neither refuses the whole send-back."""
    key = scope["key"]
    open_keys = {x["key"] for x in scope["open_links"]}
    closed_keys = {x["key"] for x in scope["closed_links"]}
    inside, outside, problems = [], [], []
    for n, f in enumerate(findings, 1):
        if "ac" in f:
            m = _AC_ONLY.fullmatch(f["ac"])
            ac = [f"AC{int(m.group(1))}"] if m else []
            if not ac:
                problems.append(f"finding {n}: «{f['ac']}» is not an AC number like AC3")
            elif not scope["acs"]:
                problems.append(
                    f"finding {n}: {key} has no numbered acceptance criteria, so nothing is sent back as changes "
                    "needed; Adam numbers them first, or it is out_of_scope on its own ticket"
                )
            elif ac[0] not in scope["acs"]:
                problems.append(
                    f"finding {n}: {ac[0]} is not one of {key}'s acceptance criteria ({', '.join(scope['acs'])}); "
                    "a finding outside them is out_of_scope on its own ticket"
                )
            else:
                inside.append({**f, "ac": ac[0]})
        else:
            other = f["out_of_scope"].upper()
            if other == key:
                problems.append(f"finding {n}: out_of_scope must name another issue, not {key}")
            elif other in open_keys:
                outside.append({**f, "out_of_scope": other})
            elif other in closed_keys:
                problems.append(f"finding {n}: {other} is linked to {key} but closed; reopen it or create a new ticket")
            else:
                problems.append(
                    f"finding {n}: {other} is not an open issue linked to {key}; create or link a ticket first "
                    f"(atl {agent} jira-create … --link {key}, or atl {agent} jira-link {key} {other})"
                )
    if problems:
        raise ScopeError("refused, nothing written: " + "; ".join(problems))
    return inside, outside


def finding_line(f: dict) -> str:
    where = f" ({f['file']})" if f.get("file") else ""
    return f"- {f.get('ac') or f.get('out_of_scope')}{where}: {f['what']}"
