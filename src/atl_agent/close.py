"""Close Jira issues when the pull request that completes them is merged.

Convention (Basita, 29/10/2026): an intermediate PR says `Refs <KEY>` in its description, and only the
final PR, the one that completes the acceptance criteria, says `Closes <KEY>`. When a PR whose
description has `Closes <KEY>` is merged, the issue moves to Done and gets a comment with the PR links.

DFP is guarded (Ahmed, 01/11/2026; operating model 4.1 item 7): there the merge comes after acceptance, so an issue
moves only from `Ready for Release`, and only to `Released`. From any other status it stays and the issue gets a
comment with the reason. Its workflow allows every status from every other, so this check is the only limit.

Reaching Cloudflare means Released (Ahmed, 01/11/2026). With --deploy-check, a guarded issue moves only once that
check succeeded on the merge commit or on a later commit of the branch, which contains it: Cloudflare builds only
the newest of quick merges, so a merge commit can have no check at all.

    python -m atl_agent.close --pr-url URL [--links-file FILE]
        [--deploy-check NAME --repo OWNER/REPO --sha SHA --branch BRANCH] < description

The description comes from stdin, the agent's token from ATL_TOKEN, the cloud id from ATL_CLOUD_ID.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from urllib.parse import quote

from .client import AtlClient, AtlError, default_cloud_id

PROJECTS = ("BASITA", "DFP", "ELB")
# Project -> (the only status a merge moves an issue from, the only status it moves it to).
GUARDED = {"DFP": ("Ready for Release", "Released")}
# A build reports about 3 minutes after the merge. Keep the workflow's timeout above this.
DEPLOY_WAIT_TRIES = 36
DEPLOY_WAIT_SECONDS = 20
NOT_CONFIRMED = "deploy not confirmed"


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


def gh_api(path: str) -> dict:
    """GET a GitHub API path. Empty on any failure, which reads as not confirmed."""
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    return json.loads(done.stdout) if done.returncode == 0 else {}


def deploy_state(api: Callable[[str], dict], repo: str, sha: str, branch: str, check: str) -> str:
    """`success` if the check succeeded on the commit or a later one, `failed` if it failed with nothing after it."""

    def runs(commit: str) -> list[dict]:
        path = f"repos/{repo}/commits/{commit}/check-runs?check_name={quote(check)}&filter=latest"
        return api(path).get("check_runs", [])

    later = [c["sha"] for c in api(f"repos/{repo}/compare/{sha}...{branch}").get("commits", [])]
    own = runs(sha)
    if any(run.get("conclusion") == "success" for commit in later for run in runs(commit)):
        return "success"
    if any(run.get("conclusion") == "success" for run in own):
        return "success"
    if not later and own and all(run.get("status") == "completed" for run in own):
        return "failed"
    return "pending"


def wait_for_deploy(state: Callable[[], str], sleep: Callable[[float], None] = time.sleep) -> bool:
    for attempt in range(DEPLOY_WAIT_TRIES):
        if attempt:
            sleep(DEPLOY_WAIT_SECONDS)
        current = state()
        if current != "pending":
            return current == "success"
    return False


def close_issues(
    client: AtlClient,
    keys: list[str],
    pr_url: str,
    links: str = "",
    deployed: Callable[[], bool] | None = None,
    deploy_check: str = "",
) -> list[str]:
    """Close each issue that is not Done yet and comment with the PR links. Returns one line per issue.

    Idempotent: an issue already in Done is left alone, so a re-run adds nothing."""
    lines = []
    confirmed = None  # asked once, and only for an issue the guard would move
    for key in keys:
        status = client.jira_status(key)
        if status["category"] == "done":
            lines.append(f"{key}: already {status['name']}, nothing to do")
            continue
        guard = GUARDED.get(key.split("-")[0])
        if guard and status["name"].lower() != guard[0].lower():
            client.jira_comment(
                key,
                f"Not moved automatically: {pr_url} was merged and its description says 'Closes {key}', but the "
                f"issue is in '{status['name']}'. A merge moves it to '{guard[1]}' only from '{guard[0]}'.",
            )
            lines.append(f"{key}: left in {status['name']}, a merge moves it only from {guard[0]}")
            continue
        if guard and deployed:
            confirmed = deployed() if confirmed is None else confirmed
            if not confirmed:
                client.jira_comment(
                    key,
                    f"Not moved to '{guard[1]}': {pr_url} was merged, but no successful '{deploy_check}' build "
                    f"contains it yet. After a good build, re-run this close job or move the issue by hand.",
                )
                lines.append(f"{key}: left in {status['name']}, {NOT_CONFIRMED}")
                continue
        new_status = client.jira_close(key, guard[1]) if guard else client.jira_close(key)
        action = f"Moved to {new_status} automatically" if guard else "Closed automatically"
        comment = f"{action}: {pr_url} was merged and its description says 'Closes {key}'."
        if links.strip():
            comment += f"\n\nPull requests that mention {key}:\n{links.strip()}"
        client.jira_comment(key, comment)
        lines.append(f"{key}: {status['name']} -> {new_status}")
    return lines


def main(argv: list[str] | None = None, stdin=None, environ=None, transport=None, api=gh_api, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="atl-close", description=__doc__.splitlines()[0])
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--links-file", help="file with one line per PR that mentions the issue")
    parser.add_argument("--agent", default="sherif", help="whose Atlassian account the token belongs to")
    parser.add_argument("--dry-run", action="store_true", help="only print the keys that would be closed")
    parser.add_argument("--deploy-check", default="", help="check run that must succeed before a guarded issue moves")
    parser.add_argument("--repo", default="")
    parser.add_argument("--sha", default="", help="the merge commit")
    parser.add_argument("--branch", default="main")
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
            default_cloud_id(environ),
            environ.get("ATL_TOKEN", ""),
            environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"),
            **kwargs,
        )
        deployed = None
        if args.deploy_check:
            deployed = lambda: wait_for_deploy(  # noqa: E731
                lambda: deploy_state(api, args.repo, args.sha, args.branch, args.deploy_check), sleep
            )
        lines = close_issues(client, keys, args.pr_url, links, deployed, args.deploy_check)
        for line in lines:
            print(line)
    except AtlError as error:
        print(f"atl-close: {error}", file=sys.stderr)
        return 1
    return 1 if any(NOT_CONFIRMED in line for line in lines) else 0


if __name__ == "__main__":
    raise SystemExit(main())
