"""Rules for agent attribution.

An agent commit is one whose author email is `<agent>@<agent domain>` for a known agent. It must
carry an `Agent: <Name>` trailer that names the same agent (scripts/agent-git adds it), and it must
not contain Jira Smart Commit commands, because those would change Jira as whoever owns the email.
A commit by a team member that carries an AI co-author or an `Agent:` trailer is reported: the
agent should have committed under its own name. Any other author is unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
# `DFP-12 ... #done` / `#comment` / `#time`: an issue key followed on the same line by #<word>.
SMART_COMMIT = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b[^\n]*?(?<![\w&/])#(?!\d)[A-Za-z][\w-]*")
TRAILER = re.compile(r"^([A-Za-z][\w-]*):\s*(.+?)\s*$")
AI_CO_AUTHOR = re.compile(r"^Co-Authored-By:\s*Claude\b", re.IGNORECASE | re.MULTILINE)
PR_AGENT_LINE = re.compile(r"^\s*Agent:\s*\w+", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Config:
    agents: frozenset[str]
    agent_domain: str
    # Team members, by email or GitHub login, lower case.
    humans: frozenset[str] = field(default_factory=frozenset)
    require_issue_key: bool = False


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    message: str
    author_login: str = ""
    parent_count: int = 1

    @classmethod
    def from_github(cls, data: dict) -> Commit:
        """Build from one item of the GitHub `pulls/{n}/commits` response."""
        author = data["commit"]["author"]
        return cls(
            sha=data["sha"],
            author_name=author.get("name") or "",
            author_email=author.get("email") or "",
            message=data["commit"].get("message") or "",
            author_login=(data.get("author") or {}).get("login") or "",
            parent_count=len(data.get("parents") or [None]),
        )


@dataclass(frozen=True)
class PullRequest:
    title: str = ""
    branch: str = ""
    body: str = ""
    author_login: str = ""

    @classmethod
    def from_github(cls, data: dict) -> PullRequest:
        return cls(
            title=data.get("title") or "",
            branch=(data.get("head") or {}).get("ref") or "",
            body=data.get("body") or "",
            author_login=(data.get("user") or {}).get("login") or "",
        )


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" or "warning"
    rule: str
    message: str
    sha: str = ""


def trailers(message: str) -> dict[str, list[str]]:
    """Trailers of the last paragraph of a commit message, keyed by lower-case name."""
    paragraphs = [p for p in message.strip().split("\n\n") if p.strip()]
    if len(paragraphs) < 2:
        return {}
    found: dict[str, list[str]] = {}
    for line in paragraphs[-1].splitlines():
        match = TRAILER.match(line)
        if match:
            found.setdefault(match.group(1).lower(), []).append(match.group(2))
    return found


def agent_of(commit: Commit, config: Config) -> str | None:
    local, _, domain = commit.author_email.lower().partition("@")
    if domain == config.agent_domain.lower() and local in config.agents:
        return local
    return None


def is_human(commit: Commit, config: Config) -> bool:
    return commit.author_email.lower() in config.humans or commit.author_login.lower() in config.humans


def check_commit(commit: Commit, config: Config) -> list[Finding]:
    if commit.parent_count > 1 or commit.author_login.endswith("[bot]"):
        return []
    short = commit.sha[:7]
    named = trailers(commit.message).get("agent", [])
    agent = agent_of(commit, config)
    if agent:
        findings = []
        if not named:
            findings.append(
                Finding(
                    "error",
                    "agent-trailer-missing",
                    f"{short}: agent commit by {commit.author_email} has no "
                    f"'Agent: {agent.capitalize()}' trailer. Commit with scripts/agent-git.",
                    commit.sha,
                )
            )
        elif any(n.lower() != agent for n in named):
            findings.append(
                Finding(
                    "error",
                    "agent-trailer-mismatch",
                    f"{short}: author is {commit.author_email} but the trailer says 'Agent: {', '.join(named)}'.",
                    commit.sha,
                )
            )
        if SMART_COMMIT.search(commit.message):
            findings.append(
                Finding(
                    "error",
                    "smart-commit",
                    f"{short}: agents must not use Jira Smart Commit commands "
                    "(#comment, #time, #<transition>). Keep the issue key, drop the command.",
                    commit.sha,
                )
            )
        return findings
    if is_human(commit, config):
        if named or AI_CO_AUTHOR.search(commit.message):
            return [
                Finding(
                    "warning",
                    "agent-commit-as-human",
                    f"{short}: AI-made commit authored as "
                    f"{commit.author_email}. Agents commit under their own name with scripts/agent-git.",
                    commit.sha,
                )
            ]
        return []
    return [
        Finding(
            "error",
            "unknown-author",
            f"{short}: author {commit.author_name} <{commit.author_email}> is neither a known agent nor a team member.",
            commit.sha,
        )
    ]


def check(commits: list[Commit], pr: PullRequest, config: Config) -> list[Finding]:
    findings = [f for c in commits for f in check_commit(c, config)]
    if not ISSUE_KEY.search(f"{pr.title} {pr.branch}"):
        severity = "error" if config.require_issue_key else "warning"
        findings.append(
            Finding(
                severity,
                "issue-key",
                "No Jira issue key (e.g. DFP-123) in the PR title or branch name, "
                "so Jira's development panel will not show this work.",
            )
        )
    has_agent_commits = any(agent_of(c, config) for c in commits)
    if has_agent_commits and pr.author_login.lower() in config.humans and not PR_AGENT_LINE.search(pr.body):
        findings.append(
            Finding(
                "warning",
                "pr-agent-line",
                f"PR opened as {pr.author_login} contains agent commits; add an "
                "'Agent: <Name>' line to the description.",
            )
        )
    return findings
