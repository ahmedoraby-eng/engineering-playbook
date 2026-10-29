"""Command line: `atl <agent> jira-comment BASITA-1 "text"` or `atl <agent> confluence-append <page id> "text"`.

Text `-` reads the text from stdin. The cloud id comes from --cloud-id or ATL_CLOUD_ID.
"""

from __future__ import annotations

import argparse
import os
import sys

from .client import AtlClient, AtlError, keychain_token


def main(argv: list[str] | None = None, token_source=keychain_token, transport=None) -> int:
    parser = argparse.ArgumentParser(prog="atl", description="Write to Jira or Confluence as a named agent.")
    parser.add_argument("agent", help="agent name, e.g. salma")
    parser.add_argument("command", choices=["jira-comment", "confluence-append"])
    parser.add_argument("target", help="issue key (jira-comment) or page id (confluence-append)")
    parser.add_argument("text", help="text to write, or - to read it from stdin")
    parser.add_argument("--cloud-id", default=os.environ.get("ATL_CLOUD_ID", ""))
    parser.add_argument("--domain", default=os.environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"))
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.text == "-" else args.text
    if not text.strip():
        print("atl: empty text", file=sys.stderr)
        return 2
    try:
        agent = args.agent.lower()
        kwargs = {"transport": transport} if transport else {}
        client = AtlClient(agent, args.cloud_id, token_source(agent), args.domain, **kwargs)
        if args.command == "jira-comment":
            result = client.jira_comment(args.target, text)
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
