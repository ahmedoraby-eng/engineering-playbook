"""Which pull requests need the architect's review, and at what effort.

The rules live in the consuming repository's `.github/review-rules.json`, the file Owner review
reads too, so there is one list of critical paths. The check reads that file from the base branch,
never from the pull request, so a PR cannot change its own rules.

Classes, first match wins:
- `waived`: the `review-waived` label, counted only if the owner added it.
- `not-required`: no commit by anyone outside `exempt_authors` (the owner's own PRs). A commit with an
  `Agent:` trailer or a `Co-Authored-By: Claude` line counts as an agent's, even under the owner's account.
- `critical`: any file, or the old name of a renamed file, on a critical path. Effort `max`.
- `not-required`: only docs (both names of a renamed file), or (only when `skip_test_only` is on,
  decision D8) only tests and docs.
- `large`: more than `large_lines` changed lines outside `size_exclude`. Effort `xhigh`.
- `normal`: everything else. Effort `high`.
The `needs-yousef` label, which anyone may add, turns `not-required` into a review.
The `minor` label (BASITA-221; Ahmed, 09/11/2026, operating model 2.2) marks a small UI or wording
change that skips the architect: `not-required`, with a note. It never outranks the rules above it: a
minor PR on a critical path (logic, data, security, calculation or CI files) stays `critical`, and a minor
PR over `large_lines` changed lines is reviewed as `large`, because anyone may add the label. `needs-yousef`
beats it. `minor_label` in the rules renames it; an empty name switches it off.
`reviewer_own_bots` lists the bots the reviewer pushes with (his docs App): his approval never counts on
a PR they opened or committed to, so he can't approve his own work (BASITA-161, plan 10.2).
A docs or test file on a critical path (an agent rule in `CLAUDE.md`, a CI file) is critical, never
docs or tests. A file list shorter than the PR's `changed_files` (GitHub stops at 3000) is critical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WAIVED, NOT_REQUIRED, CRITICAL, LARGE, NORMAL = "waived", "not-required", "critical", "large", "normal"
DEFAULT_EFFORT = {CRITICAL: "max", LARGE: "xhigh", NORMAL: "high"}


# A commit an agent made, whatever account it was pushed or authored under: `agent-git` adds the trailer,
# Claude Code adds the co-author line.
AGENT_COMMIT = re.compile(r"^(?:Agent:[ \t]*\S|Co-Authored-By:[ \t]*Claude\b)", re.IGNORECASE | re.MULTILINE)


def commit_author(commit: dict) -> str:
    """Who a commit counts as, from one item of the `pulls/{n}/commits` response.

    The GitHub login, else the author email (an unlinked author is outside any exempt list). A commit
    whose message marks it as an agent's counts as the agent's even under the owner's account."""
    author = (
        (commit.get("author") or {}).get("login")
        or ((commit.get("commit") or {}).get("author") or {}).get("email")
        or ""
    )
    if AGENT_COMMIT.search((commit.get("commit") or {}).get("message") or ""):
        return f"{author or 'unknown'} (agent commit)"
    return author


def commit_logins(commit: dict) -> set[str]:
    """The lowercased GitHub logins a commit is linked to: its author and committer. An author or
    committer address of the form `<id>+<login>@users.noreply.github.com` (what `agent-git` gives an
    App's bot) names the login even when GitHub did not link it."""
    logins = set()
    for role in ("author", "committer"):
        login = (commit.get(role) or {}).get("login") or ""
        if login:
            logins.add(login.lower())
        email = (((commit.get("commit") or {}).get(role) or {}).get("email") or "").lower()
        local, _, domain = email.partition("@")
        if domain == "users.noreply.github.com":
            logins.add(local.split("+", 1)[-1])
    return logins


class RulesError(ValueError):
    """The rules file is missing a field or holds a bad pattern."""


def _pattern(value, name: str) -> re.Pattern:
    """One regex from a list of alternatives (or a single string)."""
    parts = [value] if isinstance(value, str) else value
    if not isinstance(parts, list) or not parts or not all(isinstance(p, str) and p for p in parts):
        raise RulesError(f"'{name}' must be a non-empty list of regular expressions")
    try:
        return re.compile("|".join(f"(?:{p})" for p in parts))
    except re.error as exc:
        raise RulesError(f"'{name}' has a bad regular expression: {exc}") from exc


@dataclass(frozen=True)
class Rules:
    owner: str
    reviewer: str
    critical: re.Pattern
    size_exclude: re.Pattern
    docs: re.Pattern
    tests: re.Pattern
    exempt_authors: frozenset[str] = field(default_factory=frozenset)
    large_lines: int = 400
    skip_test_only: bool = False
    waive_label: str = "review-waived"
    needs_label: str = "needs-yousef"
    minor_label: str = "minor"
    effort: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EFFORT))
    reviewer_own_bots: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_json(cls, data: dict) -> Rules:
        if not isinstance(data, dict):
            raise RulesError("the rules file must hold a JSON object")
        arch = data.get("architecture_review")
        if not isinstance(arch, dict):
            raise RulesError("'architecture_review' is missing")
        owner = data.get("owner")
        reviewer = arch.get("reviewer")
        if not isinstance(owner, str) or not owner or not isinstance(reviewer, str) or not reviewer:
            raise RulesError("'owner' and 'architecture_review.reviewer' must be GitHub logins")
        large = arch.get("large_lines", 400)
        if not isinstance(large, int) or isinstance(large, bool) or large < 1:
            raise RulesError("'architecture_review.large_lines' must be a positive whole number")
        minor = arch.get("minor_label", "minor")
        if not isinstance(minor, str) or re.search(r"\s", minor):
            raise RulesError("'architecture_review.minor_label' must be a label name without spaces (or empty)")
        effort = {**DEFAULT_EFFORT, **(arch.get("effort") or {})}
        exempt = arch.get("exempt_authors", [owner])
        own = arch.get("reviewer_own_bots", [])
        if not isinstance(own, list) or not all(isinstance(b, str) and b for b in own):
            raise RulesError("'architecture_review.reviewer_own_bots' must be a list of GitHub logins")
        return cls(
            owner=owner,
            reviewer=reviewer,
            critical=_pattern(data.get("critical_paths"), "critical_paths"),
            size_exclude=_pattern(data.get("size_exclude"), "size_exclude"),
            docs=_pattern(arch.get("docs_paths"), "architecture_review.docs_paths"),
            tests=_pattern(arch.get("test_paths"), "architecture_review.test_paths"),
            exempt_authors=frozenset(str(a).lower() for a in exempt),
            large_lines=large,
            skip_test_only=arch.get("skip_test_only") is True,
            waive_label=arch.get("waive_label", "review-waived"),
            needs_label=arch.get("needs_label", "needs-yousef"),
            minor_label=minor,
            effort=effort,
            reviewer_own_bots=frozenset(b.lower() for b in own),
        )


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    changes: int = 0
    status: str = "modified"
    previous_filename: str = ""
    sha: str = ""

    @classmethod
    def from_github(cls, data: dict) -> ChangedFile:
        """Build from one item of the `pulls/{n}/files` or `compare` response."""
        return cls(
            filename=data["filename"],
            changes=int(data.get("changes", (data.get("additions") or 0) + (data.get("deletions") or 0))),
            status=data.get("status") or "modified",
            previous_filename=data.get("previous_filename") or "",
            sha=data.get("sha") or "",
        )

    def names(self) -> tuple[str, ...]:
        return (self.filename, self.previous_filename) if self.previous_filename else (self.filename,)


@dataclass(frozen=True)
class Classification:
    cls: str
    effort: str
    reasons: tuple[str, ...]

    @property
    def required(self) -> bool:
        return self.cls in (CRITICAL, LARGE, NORMAL)


def classify(
    rules: Rules,
    commit_authors: list[str],
    files: list[ChangedFile],
    labels: set[str],
    waived_by: str = "",
    changed_files: int | None = None,
) -> Classification:
    """Classify a PR. `commit_authors` are the GitHub logins of its commits ("" when unlinked)."""
    notes: list[str] = []
    if rules.waive_label in labels:
        if waived_by.lower() == rules.owner.lower():
            return Classification(WAIVED, "", (f"'{rules.waive_label}' added by {rules.owner}",))
        notes.append(f"'{rules.waive_label}' ignored: added by '{waived_by or 'unknown'}', not {rules.owner}")

    def done(cls: str, *reasons: str) -> Classification:
        return Classification(cls, rules.effort.get(cls, ""), (*reasons, *notes))

    others = sorted({a or "(unlinked author)" for a in commit_authors if a.lower() not in rules.exempt_authors})
    asked = rules.needs_label in labels
    if not others and not asked:
        exempt = ", ".join(sorted(rules.exempt_authors)) or "nobody"
        return Classification(NOT_REQUIRED, "", (f"no commits outside {exempt}", *notes))

    # BASITA-221: a minor PR skips the architect, but never past the critical paths, the file-list limit or the size
    # limit below, and never when the architect was asked for by name.
    minor = bool(rules.minor_label) and rules.minor_label in labels and not asked

    critical = sorted({n for f in files for n in f.names() if rules.critical.search(n)})
    if critical:
        if minor:
            notes.append(f"'{rules.minor_label}' ignored: a critical path is never minor")
        return done(CRITICAL, f"critical paths: {', '.join(critical)}")
    if changed_files is not None and changed_files > len(files):
        if minor:
            notes.append(f"'{rules.minor_label}' ignored: the file list is incomplete")
        return done(CRITICAL, f"only {len(files)} of {changed_files} changed files could be read")

    # Both names of a renamed file must match: code renamed to `docs/x.md` is not docs only.
    def only(*patterns: re.Pattern) -> bool:
        return all(any(p.search(n) for p in patterns) for f in files for n in f.names())

    skip = ""
    if only(rules.docs):
        skip = "docs only"
    elif rules.skip_test_only and only(rules.docs, rules.tests):
        skip = "tests and docs only"
    if skip and not asked:
        return Classification(NOT_REQUIRED, "", (skip, *notes))

    size = sum(f.changes for f in files if not rules.size_exclude.search(f.filename))
    if minor and size <= rules.large_lines:
        return Classification(NOT_REQUIRED, "", (f"labelled '{rules.minor_label}': no architecture review", *notes))
    if minor:
        notes.append(
            f"'{rules.minor_label}' ignored: {size} changed lines outside docs and tests (limit {rules.large_lines})"
        )
    reasons = [f"labelled '{rules.needs_label}'"] if asked else []
    if others:
        reasons.append(f"commits by {', '.join(others)}")
    if size > rules.large_lines:
        return done(LARGE, f"{size} changed lines outside docs and tests (limit {rules.large_lines})", *reasons)
    return done(NORMAL, f"{size} changed lines", *reasons)
