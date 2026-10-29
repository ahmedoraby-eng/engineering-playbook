"""Command line entry point: `python -m agent_attribution --commits commits.json --pr pr.json ...`."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

from .check import Commit, Config, Finding, PullRequest, check


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _split(value: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in value.split(",") if item.strip())


def format_finding(f: Finding, fmt: str) -> str:
    if fmt == "github":
        return f"::{f.severity} title={f.rule}::{_escape(f.message)}"
    return f"{f.severity.upper():7} [{f.rule}] {f.message}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-attribution", description="Check that every commit in a pull request names its author."
    )
    parser.add_argument("--commits", required=True, help="JSON list from the GitHub pulls/{n}/commits API")
    parser.add_argument("--pr", help="JSON pull request object (title, head.ref, body, user.login)")
    parser.add_argument("--agents", required=True, help="comma-separated agent names, e.g. malek,salma")
    parser.add_argument("--agent-domain", required=True, help="email domain of the agents")
    parser.add_argument("--humans", default="", help="comma-separated team member emails or GitHub logins")
    parser.add_argument("--require-issue-key", action="store_true", help="fail when the PR has no issue key")
    parser.add_argument("--format", choices=["text", "github"], default="text")
    parser.add_argument("--warn-only", action="store_true", help="report errors as warnings and always exit 0")
    args = parser.parse_args(argv)

    commits = [Commit.from_github(c) for c in json.loads(Path(args.commits).read_text(encoding="utf-8"))]
    pr = PullRequest.from_github(json.loads(Path(args.pr).read_text(encoding="utf-8"))) if args.pr else PullRequest()
    config = Config(
        agents=_split(args.agents),
        agent_domain=args.agent_domain.strip(),
        humans=_split(args.humans),
        require_issue_key=args.require_issue_key,
    )

    findings = check(commits, pr, config)
    if args.warn_only:
        findings = [replace(f, severity="warning") for f in findings]
    for finding in findings:
        print(format_finding(finding, args.format))
    errors = sum(f.severity == "error" for f in findings)
    print(f"agent-attribution: {len(commits)} commit(s), {errors} error(s), {len(findings) - errors} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
