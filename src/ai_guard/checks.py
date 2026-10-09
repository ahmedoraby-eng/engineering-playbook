"""The guardrail checks. Each returns a list of findings; errors fail the build."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .diff import FileChange
from .policy import Policy, matches

ALLOW_MARKER = "ai-guard: allow"

BUILTIN_SECRET_PATTERNS: dict[str, str] = {
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    "Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
    "LLM provider API key": r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b",
    "hard-coded credential": r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
}

_CHECKBOX = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ xX])\]\s*(?P<label>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" or "warning"
    rule: str
    message: str
    path: str | None = None
    line: int | None = None


def ai_disclosure(pr_body: str) -> bool | None:
    """Read the AI-assistance checkboxes from a PR description.

    Returns True (AI-assisted), False (no AI) or None (missing or ambiguous).
    """
    ticked = [m.group("label").strip().lower() for m in _CHECKBOX.finditer(pr_body or "") if m.group("mark") in "xX"]
    no_ai = any(label.startswith("no ai") for label in ticked)
    assisted = any(label.startswith("ai-assisted") for label in ticked)
    if no_ai == assisted:
        return None
    return assisted


def check_disclosure(pr_body: str) -> list[Finding]:
    if ai_disclosure(pr_body) is None:
        return [
            Finding(
                "error",
                "ai-disclosure",
                "Tick exactly one AI-assistance box in the PR description ('No AI assistance' or 'AI-assisted').",
            )
        ]
    return []


def check_secrets(changes: list[FileChange], policy: Policy) -> list[Finding]:
    patterns = dict(BUILTIN_SECRET_PATTERNS)
    for i, extra in enumerate(policy.extra_secret_patterns, start=1):
        patterns[f"custom pattern {i}"] = extra
    compiled = {name: re.compile(rx) for name, rx in patterns.items()}
    findings = []
    for change in changes:
        if matches(change.path, policy.secret_scan_ignore):
            continue
        for line_no, text in change.added:
            if ALLOW_MARKER in text:
                continue
            for name, rx in compiled.items():
                if rx.search(text):
                    findings.append(
                        Finding(
                            "error",
                            "no-secrets",
                            f"Possible {name} added. Remove it and rotate the credential.",
                            change.path,
                            line_no,
                        )
                    )
                    break
    return findings


def check_tests_accompany(changes: list[FileChange], ai_assisted: bool, policy: Policy) -> list[Finding]:
    """AI-assisted changes to production source must come with test changes."""
    if not ai_assisted:
        return []
    live = [c for c in changes if not c.deleted]
    source = [c for c in live if matches(c.path, policy.source_globs) and not matches(c.path, policy.test_globs)]
    tests = [c for c in live if matches(c.path, policy.test_globs)]
    if source and not tests:
        return [
            Finding(
                "error",
                "tests-with-ai-code",
                "AI-assisted change touches source code but adds or updates no tests.",
                source[0].path,
            )
        ]
    return []


def check_protected_paths(
    changes: list[FileChange], ai_assisted: bool, labels: set[str], policy: Policy
) -> list[Finding]:
    """AI-assisted changes to sensitive areas need an explicit human-review label."""
    if not ai_assisted or policy.human_review_label in labels:
        return []
    return [
        Finding(
            "error",
            "protected-path",
            f"AI-assisted change to a sensitive area. A reviewer must add the "
            f"'{policy.human_review_label}' label after a line-by-line review.",
            c.path,
        )
        for c in changes
        if matches(c.path, policy.protected_globs)
    ]


def check_size(changes: list[FileChange], ai_assisted: bool, policy: Policy) -> list[Finding]:
    total = sum(c.changed_lines for c in changes)
    if ai_assisted and total > policy.max_ai_changed_lines:
        return [
            Finding(
                "warning",
                "reviewable-size",
                f"AI-assisted PR changes {total} lines (guideline {policy.max_ai_changed_lines}). "
                "Consider splitting it so it can be reviewed properly.",
            )
        ]
    return []


def run_all(changes: list[FileChange], pr_body: str, labels: set[str], policy: Policy) -> list[Finding]:
    findings = check_secrets(changes, policy)
    findings += check_disclosure(pr_body)
    assisted = ai_disclosure(pr_body) is True
    findings += check_tests_accompany(changes, assisted, policy)
    findings += check_protected_paths(changes, assisted, labels, policy)
    findings += check_size(changes, assisted, policy)
    return findings
