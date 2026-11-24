"""The Fix Version rule (Ahmed, 24/11/2026, decision 21): no development issue reaches In Development, or any later
status up to Ready for Release, without an unreleased Fix Version. A Subtask uses its parent's version.

The scope (which issues the rule covers) lives in one file, config/fix-version-scope.json, so atl, the n8n backstop and
the PR gate share one list. This module reads it; nothing here talks to Jira."""

from __future__ import annotations

import json
from pathlib import Path

SCOPE_FILE = Path(__file__).resolve().parents[2] / "config" / "fix-version-scope.json"


def load_scope(path: Path | str | None = None) -> dict:
    data = json.loads(Path(path or SCOPE_FILE).read_text(encoding="utf-8"))
    for name in ("projects", "issue_types", "exclude_labels", "guarded_statuses"):
        value = data.get(name)
        if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"fix-version-scope.json: '{name}' must be a non-empty list of names")
    return data


def in_scope(key: str, issue_type: str, labels: list[str] | tuple[str, ...], scope: dict | None = None) -> bool:
    """True when the rule covers this issue: a DFP/ELB development type without a plan, milestone or decision label.
    An answer without an issue type is not in scope (Jira always sends one)."""
    scope = scope or load_scope()
    project = key.split("-")[0]
    return (
        project in scope["projects"]
        and issue_type in scope["issue_types"]
        and not set(labels or ()) & set(scope["exclude_labels"])
    )


def has_unreleased(versions: list[dict] | None) -> bool:
    return any(not v.get("released") for v in versions or [])
