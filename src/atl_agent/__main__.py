"""Command line:

    atl <agent> jira-comment BASITA-1 "text" [--mention <account id> ...]
    atl <agent> jira-start BASITA-1 ["text"]
    atl <agent> confluence-append <page id> "text"

Text `-` reads the text from stdin. The cloud id comes from --cloud-id or ATL_CLOUD_ID. Jira comments end
with an attribution line, "— <Agent> (Basita AI)", which the comment relay uses to recognise agents' own
replies. `jira-start` moves a To Do issue to In Progress, labels it `agent-picked` and comments.
"""

from __future__ import annotations

import argparse
import os
import sys

from .client import AtlClient, AtlError, keychain_token


def main(argv: list[str] | None = None, token_source=keychain_token, transport=None) -> int:
    parser = argparse.ArgumentParser(prog="atl", description="Write to Jira or Confluence as a named agent.")
    parser.add_argument("agent", help="agent name, e.g. salma")
    parser.add_argument("command", choices=["jira-comment", "jira-start", "confluence-append"])
    parser.add_argument("target", help="issue key (jira-*) or page id (confluence-append)")
    parser.add_argument("text", nargs="?", default="", help="text to write, or - to read it from stdin")
    parser.add_argument("--mention", action="append", default=[], help="account id to @mention (repeatable)")
    parser.add_argument("--cloud-id", default=os.environ.get("ATL_CLOUD_ID", ""))
    parser.add_argument("--domain", default=os.environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"))
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.text == "-" else args.text
    if args.command == "jira-start" and not text.strip():
        text = "Picked up; working on it now."
    if not text.strip():
        print("atl: empty text", file=sys.stderr)
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
