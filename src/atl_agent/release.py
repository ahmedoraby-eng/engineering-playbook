"""Release accepted DFP issues whose work is all merged (BASITA-208; Ahmed, 08/11/2026).

A DFP issue moves from `Ready for Release` to `Released` only when both hold: (a) it is in Ready for Release, so Adam
accepted it, and (b) every PR that names its key is merged, or no PR names it (design or QA work). A merge with
`Closes <KEY>` already moves it (close.py); this catches the rest: an issue whose only PR said `Refs`, and work with no
code at all. It never moves an issue from any other status, never an Epic, and never on a failed read.

It also never releases planned work (Ahmed, 08/11/2026): every Fix Version of the issue must be one of the versions
open now, given by `--versions` (basita-ops `RELEASE_CURRENT_VERSIONS` in jira-agents.conf, e.g. "0.2.1,0.2.1.2"). An
issue with no Fix Version, or with any Fix Version outside the list (a later version such as 0.2.2 means planned), is
left with the reason. An empty `--versions` releases nothing.

    python -m atl_agent.release --agent sherif --versions 0.2.1,0.2.1.2 [--dry-run] [--repos a,b,c] [--log FILE]

Run hourly by basita-ops jira-attention.sh, behind AUTO_RELEASE in jira-agents.conf. The PRs are read as basita-reader
through the playbook's agent-gh (`pr list --state all`, once per repository), never the Mac's own gh login. The token
is ATL_TOKEN when set, else the agent's Jira token from the Keychain, as atl reads it.

A PR names an issue when its title, branch or description has the key as a whole word (DFP-15 is not DFP-153),
outside HTML comments. For each issue:
  - any open PR names it: left ("open PR ...");
  - PRs name it but every one was closed without merging: left and flagged;
  - otherwise, at least one merged PR and none open, or no PR at all: moved to Released, then commented with the
    evidence. A closed-unmerged PR next to a merged one is abandoned work and doesn't count.
The status and type are read again just before the move. `--dry-run` reads only and writes nothing.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .client import PR_REPOS, AtlClient, AtlError, account_for, default_cloud_id, keychain_token, list_repo_prs
from .close import GUARDED

PROJECT = "DFP"
FROM, TO = GUARDED[PROJECT]
JQL = f'project = {PROJECT} AND status = "{FROM}" AND issuetype != Epic'
ISSUE_LIMIT = 200
FLAG = "FLAG"
LOG_HEADING = "## Released automatically (BASITA-208)"
LOG_INTRO = (
    "Moved from Ready for Release to Released by `python -m atl_agent.release` (playbook), run by "
    "`jira-attention.sh`: accepted, and every PR that names the issue is merged, or no PR names it. Each issue has a "
    "comment with the evidence."
)

PrSource = Callable[[str], list]


def names_key(pr: dict, key: str) -> bool:
    """Whether the PR names the issue: the key as a whole word in its title, branch or description, outside HTML
    comments (the PR template's own instructions). DFP-15 never matches DFP-153 or XDFP-15."""
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", re.IGNORECASE)
    body = re.sub(r"<!--.*?-->", "", pr.get("body") or "", flags=re.DOTALL)
    return any(word.search(text) for text in (pr.get("title") or "", pr.get("headRefName") or "", body))


class RepoPrs:
    """The default PR source: every PR of each repository, read once per run with `agent-gh reader pr list`, then the
    ones that name the key. A repository that fails to read fails every key, so nothing is released on a partial read.
    `gh search prs` isn't used: it can't tell merged from closed, has no branch name, matches loosely and has a low
    rate limit, and plain gh would read as Ahmed."""

    def __init__(self, repos: tuple = PR_REPOS, run: Callable = subprocess.run):
        self.repos = tuple(repos)
        self.run = run
        self._read: dict[str, list[dict] | AtlError] = {}

    def _prs(self, repo: str) -> list[dict]:
        if repo not in self._read:
            try:
                self._read[repo] = [dict(pr, repo=repo) for pr in list_repo_prs(repo, self.run)]
            except AtlError as error:
                self._read[repo] = error
        found = self._read[repo]
        if isinstance(found, AtlError):
            raise found
        return found

    def __call__(self, key: str) -> list[dict]:
        return [pr for repo in self.repos for pr in self._prs(repo) if names_key(pr, key)]


def _short(pr: dict) -> str:
    return f"{str(pr.get('repo') or '').split('/')[-1]}#{pr.get('number')}"


def _link(pr: dict) -> str:
    return pr.get("url") or _short(pr)


def decide(prs: list[dict]) -> tuple[str, str]:
    """("release", "") or ("leave", reason) or (FLAG, reason) for the PRs that name an issue."""
    unknown = [p for p in prs if p.get("state") not in ("OPEN", "MERGED", "CLOSED")]
    if unknown:
        return "leave", f"PR {_short(unknown[0])} is in an unknown state ({unknown[0].get('state')})"
    still_open = [p for p in prs if p["state"] == "OPEN"]
    if still_open:
        return "leave", "open PR " + ", ".join(_short(p) for p in still_open)
    if prs and not any(p["state"] == "MERGED" for p in prs):
        closed = ", ".join(_short(p) for p in prs)
        return FLAG, f"its PRs were closed without merging ({closed}); check it with Adam"
    return "release", ""


def evidence(key: str, merged: list[dict]) -> str:
    """The comment posted after the move, without the signature."""
    head = f"Released automatically (BASITA-208): accepted ({FROM}) and "
    if not merged:
        return head + f"no PR names {key} (design or QA work)."
    return head + "every PR that names it is merged:\n" + "\n".join(_link(p) for p in merged)


def _now_status(client: AtlClient, key: str) -> tuple[str, str, list[str]]:
    """The issue's status, type and Fix Versions, read again now."""
    found = client.jira_jql(f"key = {key}", limit=1)["issues"]
    if not found:
        raise AtlError(f"{key}: not found when read again")
    return found[0]["status"], found[0]["type"], list(found[0].get("fix_versions") or [])


def parse_versions(text: str) -> frozenset[str]:
    """The current versions from a comma-separated list; blanks are dropped."""
    return frozenset(v.strip() for v in (text or "").split(",") if v.strip())


def version_gate(fix_versions: list[str], versions: frozenset[str]) -> str:
    """Why the Fix Versions keep the issue from release, or "" when they allow it: at least one, and every one of
    them a current version."""
    if not versions:
        return "no current versions are configured (--versions), nothing is released"
    if not fix_versions:
        return "it has no Fix Version"
    planned = [v for v in fix_versions if v not in versions]
    if planned:
        current = ", ".join(sorted(versions))
        return f"Fix Version {', '.join(planned)} is not a current version ({current}), so it is planned work"
    return ""


def release_issues(
    client: AtlClient,
    prs_for: PrSource,
    dry_run: bool = False,
    issues: list[dict] | None = None,
    versions: frozenset[str] = frozenset(),
) -> tuple[list[str], list[str], bool]:
    """Release what qualifies. Returns (one line per issue, the log rows of the moves, whether anything failed).
    `versions` are the versions open now; with none, nothing is released."""
    issues = client.jira_jql(JQL, limit=ISSUE_LIMIT)["issues"] if issues is None else issues
    lines: list[str] = []
    moved: list[str] = []
    failed = False
    for issue in issues:
        key = issue["key"]
        if not key.startswith(f"{PROJECT}-"):
            lines.append(f"{key}: left, not a {PROJECT} issue")
            continue
        if issue.get("type") == "Epic":
            lines.append(f"{key}: left, an Epic is never released automatically")
            continue
        if (issue.get("status") or "").lower() != FROM.lower():
            lines.append(f"{key}: left in {issue.get('status')}, only {FROM} moves")
            continue
        held = version_gate(list(issue.get("fix_versions") or []), versions)
        if held:
            lines.append(f"{key}: left, {held}")
            continue
        try:
            prs = [p for p in prs_for(key) if names_key(p, key)]
        except AtlError as error:
            lines.append(f"{key}: left, the PR search failed ({error})")
            failed = True
            continue
        verdict, reason = decide(prs)
        if verdict == FLAG:
            lines.append(f"{key}: {FLAG} left in {FROM}, {reason}")
            continue
        if verdict == "leave":
            lines.append(f"{key}: left, {reason}")
            continue
        merged = [p for p in prs if p["state"] == "MERGED"]
        shown = ", ".join(_short(p) for p in merged) or "no PR"
        try:
            status, kind, fix_now = _now_status(client, key)
        except AtlError as error:
            lines.append(f"{key}: left, could not read it again ({error})")
            failed = True
            continue
        if status.lower() != FROM.lower():
            lines.append(f"{key}: left, now in {status}")
            continue
        if kind == "Epic":
            lines.append(f"{key}: left, an Epic is never released automatically")
            continue
        held = version_gate(fix_now, versions)
        if held:
            lines.append(f"{key}: left, {held} (read again)")
            continue
        if dry_run:
            lines.append(f"{key}: would move {FROM} -> {TO} ({shown})")
            continue
        try:
            new_status = client.jira_close(key, TO)
        except AtlError as error:
            lines.append(f"{key}: left, the move failed ({error})")
            failed = True
            continue
        when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        links = ", ".join(f"[{_short(p)}]({p['url']})" if p.get("url") else _short(p) for p in merged) or "no PR"
        moved.append(f"| {when} | {key} | {FROM} -> {new_status} | {links} |")
        try:
            client.jira_comment(key, f"{evidence(key, merged)}\n\n— {client.signature}")
        except AtlError as error:
            lines.append(f"{key}: {FROM} -> {new_status} ({shown}), but the comment failed ({error})")
            failed = True
            continue
        lines.append(f"{key}: {FROM} -> {new_status} ({shown})")
    return lines, moved, failed


def append_log(path: str, rows: list[str]) -> None:
    """Append the moves to the log file's own section, the last one in the file; add the section when it is missing."""
    if not rows:
        return
    file = Path(path)
    text = file.read_text(encoding="utf-8") if file.exists() else ""
    with file.open("a", encoding="utf-8") as handle:
        if LOG_HEADING not in text.splitlines():
            if text and not text.endswith("\n"):
                handle.write("\n")
            handle.write(f"\n{LOG_HEADING}\n\n{LOG_INTRO}\n\n| When | Issue | Move | Merged PRs |\n|---|---|---|---|\n")
        handle.write("".join(f"{row}\n" for row in rows))


def main(
    argv: list[str] | None = None,
    environ=None,
    transport=None,
    token_source=keychain_token,
    prs_for: PrSource | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="atl-release", description=__doc__.splitlines()[0])
    parser.add_argument("--agent", required=True, help="whose Atlassian account moves and comments, e.g. sherif")
    parser.add_argument("--dry-run", action="store_true", help="read only: print what would move, write nothing")
    parser.add_argument("--repos", default=",".join(PR_REPOS), help="owner/repo list, comma-separated")
    parser.add_argument("--log", default="", help="append the moves to this Markdown file")
    parser.add_argument(
        "--versions",
        required=True,
        help="the versions open now, comma-separated (e.g. 0.2.1,0.2.1.2); only issues whose every Fix Version is one "
        "of them are released",
    )
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    repos = tuple(r.strip() for r in args.repos.split(",") if r.strip())
    if not repos:
        print("atl-release: --repos is empty, nothing read", file=sys.stderr)
        return 2
    versions = parse_versions(args.versions)
    if not versions:
        print("atl-release: --versions is empty, nothing released", file=sys.stderr)
        return 2
    try:
        agent = args.agent.lower()
        token = environ.get("ATL_TOKEN") or token_source(account_for(agent), "jira")
        kwargs = {"transport": transport} if transport else {}
        client = AtlClient(
            agent, default_cloud_id(environ), token, environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"), **kwargs
        )
        lines, moved, failed = release_issues(client, prs_for or RepoPrs(repos), args.dry_run, versions=versions)
    except AtlError as error:
        print(f"atl-release: {error}", file=sys.stderr)
        return 1
    mode = "dry run, nothing written" if args.dry_run else f"{len(moved)} moved"
    print(f"atl-release: {len(lines)} {FROM} issue(s), {mode}")
    for line in lines:
        print(line)
    if args.log and not args.dry_run:
        try:
            append_log(args.log, moved)
        except OSError as error:
            print(f"atl-release: the moves happened but {args.log} could not be written ({error})", file=sys.stderr)
            return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
