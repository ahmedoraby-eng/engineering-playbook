"""Command line:

    atl <agent> jira-get BASITA-1
    atl <agent> jira-jql "project = DFP AND status = 'To Do'"
    atl <agent> jira-comment BASITA-1 "text" [--mention <account id> ...]
    atl <agent> jira-start BASITA-1 ["text"]
    atl <agent> jira-ready BASITA-1 ["text"]
    atl adam jira-return DFP-1 "reason"
    atl salma jira-dismiss DFP-1 --reason "Not a Bug" ["text"]
    atl <agent> confluence-get <page id or URL>
    atl <agent> confluence-search "words or CQL"
    atl <agent> confluence-create <space key> "text" --title "Title" [--parent <page id>] [--storage]
    atl <agent> confluence-edit <page id or URL> "text" [--title "New title"] [--storage]
    atl <agent> confluence-append <page id> "text"

An agent with no Atlassian user (Yousef, Basel), or one given --via talia, uses Talia's user and token, and signs
"— Yousef (Basita AI, via Talia)".

Text `-` reads the text from stdin. The cloud id comes from --cloud-id or ATL_CLOUD_ID, and defaults to Basita's
site. Jira comments end with an attribution line, "— <Agent> (Basita AI)", which the comment relay uses to
recognise agents' own replies. `jira-start` moves a To Do issue to In Progress, labels it `agent-picked` and
comments. `jira-ready` moves a Refinement issue to Ready (never an Epic) and comments. The read commands print
JSON and change nothing: `jira-get` gives an issue with its comments, sub-tasks and linked issues; `jira-jql` runs a
JQL query; `confluence-get` gives a page with its body, comments, attachments and children; `confluence-search` finds
pages by words or CQL. Each result names the agent whose token answered (`read_by`). `confluence-create` and
`confluence-edit` take plain text, or Confluence storage format with --storage. `confluence-edit` replaces the body;
Confluence keeps every earlier version. Nothing here deletes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .client import CLOSE_REASONS, AtlClient, AtlError, account_for, default_cloud_id, keychain_token

READS = {
    "jira-get": lambda c: c.jira_get,
    "jira-jql": lambda c: c.jira_jql,
    "confluence-get": lambda c: c.confluence_get,
    "confluence-search": lambda c: c.confluence_search,
}


def main(argv: list[str] | None = None, token_source=keychain_token, transport=None) -> int:
    parser = argparse.ArgumentParser(prog="atl", description="Read and write Jira and Confluence as a named agent.")
    parser.add_argument("agent", help="agent name, e.g. salma")
    parser.add_argument(
        "command",
        choices=[
            "jira-get",
            "jira-jql",
            "jira-comment",
            "jira-start",
            "jira-ready",
            "jira-return",
            "jira-dismiss",
            "confluence-get",
            "confluence-search",
            "confluence-create",
            "confluence-edit",
            "confluence-append",
        ],
    )
    parser.add_argument("target", help="issue key, JQL query, page id or URL, space key or search words, by command")
    parser.add_argument("text", nargs="?", default="", help="text to write, or - to read it from stdin")
    parser.add_argument("--mention", action="append", default=[], help="account id to @mention (repeatable)")
    parser.add_argument("--reason", choices=CLOSE_REASONS, help="jira-dismiss: why the issue is closed")
    parser.add_argument("--title", default="", help="confluence-create, confluence-edit: the page title")
    parser.add_argument("--parent", default="", help="confluence-create: the parent page's id or URL")
    parser.add_argument("--storage", action="store_true", help="the text is Confluence storage format")
    parser.add_argument("--via", default="", help="the agent whose Atlassian user to work through, e.g. talia")
    parser.add_argument("--cloud-id", default=default_cloud_id())
    parser.add_argument("--domain", default=os.environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"))
    args = parser.parse_args(argv)

    if args.command in READS:
        try:
            agent = args.agent.lower()
            kwargs = {"transport": transport} if transport else {}
            product = args.command.split("-")[0]
            client = AtlClient(
                agent,
                args.cloud_id,
                token_source(account_for(agent, args.via), product),
                args.domain,
                via=args.via,
                **kwargs,
            )
            print(json.dumps(READS[args.command](client)(args.target), ensure_ascii=False, indent=2))
        except AtlError as error:
            print(f"atl: {error}", file=sys.stderr)
            return 1
        return 0

    text = sys.stdin.read() if args.text == "-" else args.text
    if args.command in ("confluence-create", "confluence-edit"):
        if args.command == "confluence-create" and not (args.title.strip() and text.strip()):
            print("atl: confluence-create needs --title and the text", file=sys.stderr)
            return 2
        try:
            agent = args.agent.lower()
            kwargs = {"transport": transport} if transport else {}
            client = AtlClient(
                agent,
                args.cloud_id,
                token_source(account_for(agent, args.via), "confluence"),
                args.domain,
                via=args.via,
                **kwargs,
            )
            if args.command == "confluence-create":
                result = client.confluence_create(args.target, args.title.strip(), text, args.parent, args.storage)
            else:
                result = client.confluence_edit(args.target, text, args.title, args.storage)
            links = result.get("_links") or {}
            url = (links.get("base") or "") + (links.get("webui") or "")
            version = (result.get("version") or {}).get("number")
            print(f"page {result.get('id')} version {version} by {client.display} {url}".rstrip())
        except AtlError as error:
            print(f"atl: {error}", file=sys.stderr)
            return 1
        return 0
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
        product = args.command.split("-")[0]
        client = AtlClient(
            agent,
            args.cloud_id,
            token_source(account_for(agent, args.via), product),
            args.domain,
            via=args.via,
            **kwargs,
        )
        signed = f"{text.rstrip()}\n\n— {client.signature}"
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
