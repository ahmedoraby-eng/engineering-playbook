"""Command line:

    atl <agent> jira-read BASITA-1
    atl <agent> jira-search "project = DFP AND status = 'To Do'"
    atl <agent> confluence-read <page id or URL>
    atl <agent> confluence-search "words or CQL"
    atl <agent> jira-comment BASITA-1 "text" [--mention <account id> ...]
    atl <agent> jira-start BASITA-1 ["text"]
    atl <agent> jira-ready BASITA-1 ["text"]
    atl adam jira-return DFP-1 "reason"
    atl salma jira-dismiss DFP-1 --reason "Not a Bug" ["text"]
    atl <agent> confluence-append <page id> "text"

Text `-` reads the text from stdin. The cloud id comes from --cloud-id or ATL_CLOUD_ID, and defaults to Basita's
site. Jira comments end with an attribution line, "— <Agent> (Basita AI)", which the comment relay uses to
recognise agents' own replies. `jira-start` moves a To Do issue to In Progress, labels it `agent-picked` and
comments. `jira-ready` moves a Refinement issue to Ready (never an Epic) and comments. The read commands print
JSON and change nothing: `jira-read` gives an issue with its comments, sub-tasks and linked issues; `jira-search`
runs a JQL query; `confluence-read` gives a page with its body, comments and children; `confluence-search` finds
pages by words or CQL. Every read names the agent whose token answered (`read_by`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .client import CLOSE_REASONS, AtlClient, AtlError, default_cloud_id, keychain_token


READS = {
    "jira-read": lambda c: c.jira_read,
    "jira-search": lambda c: c.jira_search,
    "confluence-read": lambda c: c.confluence_read,
    "confluence-search": lambda c: c.confluence_search,
}


def main(argv: list[str] | None = None, token_source=keychain_token, transport=None) -> int:
    parser = argparse.ArgumentParser(
        prog="atl", description="Read Jira and Confluence, or write to them, as a named agent."
    )
    parser.add_argument("agent", help="agent name, e.g. salma")
    parser.add_argument(
        "command",
        choices=[
            "jira-read",
            "jira-search",
            "confluence-read",
            "confluence-search",
            "jira-comment",
            "jira-start",
            "jira-ready",
            "jira-return",
            "jira-dismiss",
            "confluence-append",
        ],
    )
    parser.add_argument("target", help="issue key, JQL, page id or URL, or search words, by command")
    parser.add_argument("text", nargs="?", default="", help="text to write, or - to read it from stdin")
    parser.add_argument("--mention", action="append", default=[], help="account id to @mention (repeatable)")
    parser.add_argument("--reason", choices=CLOSE_REASONS, help="jira-dismiss: why the issue is closed")
    parser.add_argument("--cloud-id", default=default_cloud_id())
    parser.add_argument("--domain", default=os.environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"))
    args = parser.parse_args(argv)

    if args.command in READS:
        try:
            agent = args.agent.lower()
            kwargs = {"transport": transport} if transport else {}
            product = args.command.split("-")[0]
            client = AtlClient(agent, args.cloud_id, token_source(agent, product), args.domain, **kwargs)
            print(json.dumps(READS[args.command](client)(args.target), ensure_ascii=False, indent=2))
        except AtlError as error:
            print(f"atl: {error}", file=sys.stderr)
            return 1
        return 0

    text = sys.stdin.read() if args.text == "-" else args.text
    if args.command == "jira-start" and not text.strip():
        text = "Picked up; working on it now."
    if args.command == "jira-ready" and not text.strip():
        text = "Refined; moving to Ready."
    if args.command == "jira-dismiss":
        if not args.reason:
            print("atl: jira-dismiss needs --reason", file=sys.stderr)
            return 2
        text = f"Closed: {args.reason}." + (f" {text.strip()}" if text.strip() else "")
    if not text.strip():
        print("atl: empty text (jira-return needs the reason)", file=sys.stderr)
        return 2
    try:
        agent = args.agent.lower()
        kwargs = {"transport": transport} if transport else {}
        product = "confluence" if args.command == "confluence-append" else "jira"
        client = AtlClient(agent, args.cloud_id, token_source(agent, product), args.domain, **kwargs)
        signed = f"{text.rstrip()}\n\n— {client.display} (Basita AI)"
        if args.command == "jira-start":
            status = client.jira_start(args.target)
            client.jira_comment(args.target, signed)
            print(f"{args.target} -> {status}, labelled agent-picked, by {client.display}")
        elif args.command == "jira-ready":
            status = client.jira_ready(args.target)
            client.jira_comment(args.target, signed)
            print(f"{args.target} -> {status}, by {client.display}")
        elif args.command == "jira-return":
            status = client.jira_return(args.target)
            client.jira_comment(args.target, signed)
            print(f"{args.target} -> {status}, assigned to Salma, by {client.display}")
        elif args.command == "jira-dismiss":
            status = client.jira_dismiss(args.target, args.reason)
            client.jira_comment(args.target, signed)
            print(f"{args.target} -> {status} ({args.reason}), by {client.display}")
        elif args.command == "jira-comment":
            result = client.jira_comment(args.target, signed, tuple(args.mention))
            print(f"comment {result.get('id')} on {args.target} by {result.get('author', {}).get('displayName')}")
        else:
            result = client.confluence_append(args.target, text)
            print(f"page {args.target} now version {result.get('version', {}).get('number')} by {client.display}")
    except AtlError as error:
        print(f"atl: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
