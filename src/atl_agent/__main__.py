"""Command line:

    atl <agent> jira-read BASITA-1
    atl <agent> jira-comment BASITA-1 "text" [--mention <account id> ...]
    atl <agent> jira-start BASITA-1 ["text"]
    atl <agent> jira-ready BASITA-1 ["text"]
    atl adam jira-return DFP-1 "reason"
    atl salma jira-dismiss DFP-1 --reason "Not a Bug" ["text"]
    atl <agent> confluence-append <page id> "text"

Text `-` reads the text from stdin. The cloud id comes from --cloud-id or ATL_CLOUD_ID, and defaults to Basita's
site. Jira comments end with an attribution line, "— <Agent> (Basita AI)", which the comment relay uses to
recognise agents' own replies. `jira-start` moves a To Do issue to In Progress, labels it `agent-picked` and
comments. `jira-ready` moves a Refinement issue to Ready (never an Epic) and comments. `jira-read` prints the
issue, its comments and its linked issues as JSON, and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .client import CLOSE_REASONS, AtlClient, AtlError, default_cloud_id, keychain_token


def main(argv: list[str] | None = None, token_source=keychain_token, transport=None) -> int:
    parser = argparse.ArgumentParser(prog="atl", description="Read Jira, or write to Jira or Confluence, as a named agent.")
    parser.add_argument("agent", help="agent name, e.g. salma")
    parser.add_argument(
        "command",
        choices=["jira-read", "jira-comment", "jira-start", "jira-ready", "jira-return", "jira-dismiss", "confluence-append"],
    )
    parser.add_argument("target", help="issue key (jira-*) or page id (confluence-append)")
    parser.add_argument("text", nargs="?", default="", help="text to write, or - to read it from stdin")
    parser.add_argument("--mention", action="append", default=[], help="account id to @mention (repeatable)")
    parser.add_argument("--reason", choices=CLOSE_REASONS, help="jira-dismiss: why the issue is closed")
    parser.add_argument("--cloud-id", default=default_cloud_id())
    parser.add_argument("--domain", default=os.environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"))
    args = parser.parse_args(argv)

    if args.command == "jira-read":
        try:
            agent = args.agent.lower()
            kwargs = {"transport": transport} if transport else {}
            client = AtlClient(agent, args.cloud_id, token_source(agent, "jira"), args.domain, **kwargs)
            print(json.dumps(client.jira_read(args.target), ensure_ascii=False, indent=2))
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
