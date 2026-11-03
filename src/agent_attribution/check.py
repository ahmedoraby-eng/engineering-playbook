"""Rules for agent attribution.

An agent commit is one whose author email is `<agent>@<agent domain>` for a known agent. It must
carry an `Agent: <Name>` trailer that names the same agent (scripts/agent-git adds it), and it must
not contain Jira Smart Commit commands, because those would change Jira as whoever owns the email.
A commit by a team member that carries an AI co-author or an `Agent:` trailer is reported: the
agent should have committed under its own name. Any other author is unknown.

An agent with a GitHub App commits as its bot: the author login is `<slug>[bot]`, or the email is
`<id>+<slug>[bot]@users.noreply.github.com` when GitHub does not resolve the login. Such a commit
must carry an `Agent:` trailer naming the agent the bot belongs to (Yousef's documents App,
basita-yousef-docs[bot], belongs to Yousef), and no Smart Commit commands. A
pull request opened by an agent's bot must not name another agent, in its `Agent:` line or through
its commits. The bot findings are warnings in the trial mode and errors once it ends. Other bots,
such as dependabot, are skipped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
# `DFP-12 ... #done` / `#comment` / `#time`: an issue key followed on the same line by #<word>.
SMART_COMMIT = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b[^\n]*?(?<![\w&/])#(?!\d)[A-Za-z][\w-]*")
TRAILER = re.compile(r"^([A-Za-z][\w-]*):\s*(.+?)\s*$")
AI_CO_AUTHOR = re.compile(r"^Co-Authored-By:\s*Claude\b", re.IGNORECASE | re.MULTILINE)
PR_AGENT_LINE = re.compile(r"^\s*Agent:\s*(\w+)", re.IGNORECASE | re.MULTILINE)
BOT_EMAIL = re.compile(r"^\d+\+([^@\s]+\[bot\])@users\.noreply\.github\.com$", re.IGNORECASE)


@dataclass(frozen=True)
class Config:
    agents: frozenset[str]
    agent_domain: str
    # Team members, by email or GitHub login, lower case.
    humans: frozenset[str] = field(default_factory=frozenset)
    require_issue_key: bool = False
    # GitHub App bot logins, lower case (e.g. "basita-malek[bot]"), to the agent they belong to.
    bots: Mapping[str, str] = field(default_factory=dict, hash=False)
    # "warning" during the trial, "error" once bot attribution is enforced.
    bot_severity: str = "warning"


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


def bot_agent_of(commit: Commit, config: Config) -> str | None:
    """The agent whose GitHub App bot made the commit, by login or by its noreply email."""
    agent = config.bots.get(commit.author_login.lower())
    if agent:
        return agent
    match = BOT_EMAIL.match(commit.author_email.strip())
    return config.bots.get(match.group(1).lower()) if match else None


def commit_agent(commit: Commit, config: Config) -> str | None:
    return agent_of(commit, config) or bot_agent_of(commit, config)


def _smart_commit(commit: Commit) -> list[Finding]:
    if not SMART_COMMIT.search(commit.message):
        return []
    return [
        Finding(
            "error",
            "smart-commit",
            f"{commit.sha[:7]}: agents must not use Jira Smart Commit commands "
            "(#comment, #time, #<transition>). Keep the issue key, drop the command.",
            commit.sha,
        )
    ]


def _check_bot_commit(commit: Commit, agent: str, config: Config) -> list[Finding]:
    short = commit.sha[:7]
    login = commit.author_login or commit.author_email
    named = trailers(commit.message).get("agent", [])
    findings = []
    if not named:
        findings.append(
            Finding(
                config.bot_severity,
                "bot-trailer-missing",
                f"{short}: commit by {login} has no 'Agent: {agent.capitalize()}' trailer.",
                commit.sha,
            )
        )
    elif any(n.lower() != agent for n in named):
        findings.append(
            Finding(
                config.bot_severity,
                "bot-trailer-mismatch",
                f"{short}: commit by {login} ({agent.capitalize()}'s bot) but the trailer says "
                f"'Agent: {', '.join(named)}'.",
                commit.sha,
            )
        )
    return findings + _smart_commit(commit)


def is_human(commit: Commit, config: Config) -> bool:
    return commit.author_email.lower() in config.humans or commit.author_login.lower() in config.humans


def check_commit(commit: Commit, config: Config) -> list[Finding]:
    if commit.parent_count > 1:
        return []
    bot_agent = bot_agent_of(commit, config)
    if bot_agent:
        return _check_bot_commit(commit, bot_agent, config)
    if commit.author_login.lower().endswith("[bot]"):
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
        return findings + _smart_commit(commit)
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
    commit_agents = {a for a in (commit_agent(c, config) for c in commits) if a}
    if commit_agents and pr.author_login.lower() in config.humans and not PR_AGENT_LINE.search(pr.body):
        findings.append(
            Finding(
                "warning",
                "pr-agent-line",
                f"PR opened as {pr.author_login} contains agent commits; add an "
                "'Agent: <Name>' line to the description.",
            )
        )
    return findings + check_bot_pr(pr, commit_agents, config)


def check_bot_pr(pr: PullRequest, commit_agents: set[str], config: Config) -> list[Finding]:
    """A pull request opened by an agent's bot must not name, or contain commits of, another agent."""
    agent = config.bots.get(pr.author_login.lower())
    if not agent:
        return []
    findings = []
    named = sorted({n for n in PR_AGENT_LINE.findall(pr.body) if n.lower() != agent})
    if named:
        findings.append(
            Finding(
                config.bot_severity,
                "pr-bot-mismatch",
                f"PR opened by {pr.author_login} ({agent.capitalize()}'s bot) but the description says "
                f"'Agent: {', '.join(named)}'.",
            )
        )
    others = sorted(commit_agents - {agent})
    if others:
        findings.append(
            Finding(
                config.bot_severity,
                "pr-bot-mismatch",
                f"PR opened by {pr.author_login} ({agent.capitalize()}'s bot) contains commits by "
                f"{', '.join(o.capitalize() for o in others)}.",
            )
        )
    return findings
