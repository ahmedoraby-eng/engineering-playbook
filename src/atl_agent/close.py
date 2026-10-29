"""Close Jira issues when the pull request that completes them is merged.

Convention (Basita, 29/10/2026): an intermediate PR says `Refs <KEY>` in its description, and only the
final PR, the one that completes the acceptance criteria, says `Closes <KEY>`. When a PR whose
description has `Closes <KEY>` is merged, the issue moves to Done and gets a comment with the PR links.

    python -m atl_agent.close --pr-url URL [--links-file FILE] < description

The description comes from stdin, the agent's token from ATL_TOKEN, the cloud id from ATL_CLOUD_ID.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from .client import REVIEW_LABEL, AtlClient, AtlError

PROJECTS = ("BASITA", "DFP", "ELB")


def closing_keys(description: str, projects: tuple[str, ...] = PROJECTS) -> list[str]:
    """Issue keys named by `Closes <KEY>` in a PR description, in order, without duplicates.

    HTML comments are ignored so the PR template's own instructions never close anything.
    Only `Closes` counts: `Refs <KEY>` is a plain reference and `Fixes`/`Resolves` are not used here."""
    text = re.sub(r"<!--.*?-->", "", description or "", flags=re.DOTALL)
    pattern = rf"(?<![\w-])closes:?\s+((?:{'|'.join(map(re.escape, projects))})-\d+)\b"
    keys: list[str] = []
    for key in re.findall(pattern, text, flags=re.IGNORECASE):
        key = key.upper()
        if key not in keys:
            keys.append(key)
    return keys


def close_issues(client: AtlClient, keys: list[str], pr_url: str, links: str = "") -> list[str]:
    """Close each issue that is not Done yet and comment with the PR links. Returns one line per issue.

    Idempotent: an issue already in Done is left alone, so a re-run adds nothing."""
    lines = []
    for key in keys:
        status = client.jira_status(key)
        if status["category"] == "done":
            lines.append(f"{key}: already {status['name']}, nothing to do")
            continue
        if REVIEW_LABEL in status.get("labels", []):
            # Human in the loop: a person approves, then closes it (Ahmed, 30/10/2026).
            client.jira_comment(key, f"Not closed: {pr_url} was merged, but {key} is awaiting a person's approval.")
            lines.append(f"{key}: awaiting approval, left open")
            continue
        new_status = client.jira_close(key)
        comment = f"Closed automatically: {pr_url} was merged and its description says 'Closes {key}'."
        if links.strip():
            comment += f"\n\nPull requests that mention {key}:\n{links.strip()}"
        client.jira_comment(key, comment)
        lines.append(f"{key}: {status['name']} -> {new_status}")
    return lines


def main(argv: list[str] | None = None, stdin=None, environ=None, transport=None) -> int:
    parser = argparse.ArgumentParser(prog="atl-close", description=__doc__.splitlines()[0])
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--links-file", help="file with one line per PR that mentions the issue")
    parser.add_argument("--agent", default="talia", help="whose Atlassian account the token belongs to")
    parser.add_argument("--dry-run", action="store_true", help="only print the keys that would be closed")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    keys = closing_keys((stdin or sys.stdin).read())
    if not keys:
        print("No 'Closes <KEY>' in the description: nothing to close.")
        return 0
    if args.dry_run:
        print("Would close: " + ", ".join(keys))
        return 0
    links = ""
    if args.links_file and os.path.exists(args.links_file):
        with open(args.links_file, encoding="utf-8") as handle:
            links = handle.read()
    try:
        kwargs = {"transport": transport} if transport else {}
        client = AtlClient(
            args.agent,
            environ.get("ATL_CLOUD_ID", ""),
            environ.get("ATL_TOKEN", ""),
            environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"),
            **kwargs,
        )
        for line in close_issues(client, keys, args.pr_url, links):
            print(line)
    except AtlError as error:
        print(f"atl-close: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
