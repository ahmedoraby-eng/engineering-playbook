"""Command line:

    atl <agent> jira-get BASITA-1 [--json] [--out DIR]
    atl <agent> jira-md BASITA-1 [--out DIR]
    atl <agent> jira-jql "project = DFP AND status = 'To Do'"
    atl <agent> jira-comment BASITA-1 "text" [--mention <account id> ...]
    atl <agent> jira-start BASITA-1 ["text"]
    atl <agent> jira-done BASITA-1 --why "what was delivered"
    atl <agent> jira-ready BASITA-1 ["text"]
    atl adam jira-return DFP-1 "reason"
    atl salma jira-dismiss DFP-1 --reason "Not a Bug" ["text"]
    atl <agent> jira-edit DFP-1 [--add-label L] [--remove-label L] [--set-labels "a,b"] [--assignee <name|none>]
        [--description "text"] [--priority High] [--fix-version V] [--remove-fix-version V] [--set-fix-versions "a,b"]
        [--summary "text"] [--parent DFP-2] [--due-date YYYY-MM-DD|none]
    atl <agent> jira-link DFP-1 DFP-2 [--type "blocks"]
    atl <agent> confluence-get <page id or URL> [--storage]
    atl <agent> confluence-search "words or CQL"
    atl <agent> confluence-create <space key> "text" --title "Title" [--parent <page id>] [--storage] [--label L ...]
    atl <agent> confluence-edit <page id or URL> ["text"] [--title "New title"] [--storage] [--parent <page id>]
        [--label L ...]
    atl <agent> confluence-attach <page id or URL> <file>
    atl <agent> confluence-append <page id> "text"

--via <agent> uses that agent's user and token for the command, and signs "— Maya (Basita AI, via Sherif)". Maya has
no Jira seat, so her Jira commands take --via sherif; her Confluence commands don't, because she has her own user there.

Text `-` reads the text from stdin. The cloud id comes from --cloud-id or ATL_CLOUD_ID, and defaults to Basita's
site. Jira comments end with an attribution line, "— <Agent> (Basita AI)", which the comment relay uses to
recognise agents' own replies. `jira-start` moves a To Do issue to In Progress, labels it `agent-picked` and
comments. `jira-done <KEY> --why "..."` moves the agent's own BASITA or ELB issue from In Progress to Done
(BASITA-178), for work with no open PR: it refuses DFP, an Epic, someone else's issue, `needs-ahmed`, any open PR
that mentions the issue (PR work closes through `Closes` on merge) and PRs it can't read; it comments, then moves,
and changes no label. Maya's issues (assigned to Sherif, labelled agent-maya) take --via sherif. `jira-ready` moves
a Refinement issue to Ready (never an Epic) and comments.
`jira-create <PROJECT> <Story|Task|Bug|Sub-task|Epic> "summary" "description"` creates an issue, signed in its
description and unassigned unless --assignee says otherwise.
`jira-edit <KEY>` changes the fields named by its options in one request, as the agent's own user (BASITA-169):
labels and Fix Versions are added, removed or set as a whole list (comma-separated; "" empties it); the labels only
Ahmed sets (reviewed-by-ahmed, human-reviewed, needs-ahmed, jira-status-waived, any case) are refused in every form,
and a whole list must keep them exactly as they are, case included. Issues are in BASITA, DFP or ELB. `--assignee`
takes a name with a Jira seat or `none`, for the agents that assign; `--description` replaces the description and
signs it (`-` reads stdin); `--due-date` takes YYYY-MM-DD or `none`. `jira-link <KEY> <KEY> --type T` links two
issues so that the first one "<T>" the second: T is a link type's name or phrase (relates to, blocks, is blocked by,
duplicates, clones; default relates to). It reads the link back and prints what this call recorded, compared with the
links there before.
`jira-flow <KEY> <action> "why"` makes one step of DFP's flow (submit, pass, fail, accept, reject, and approve once
it is switched on): the table in client.py gives who may, the next status and the next assignee. With FLOW_REVIEW_GUARD
on in basita-ops jira-agents.conf, submit waits for Architecture review green on the head of every open PR that
mentions the issue; approve (Yousef's, FLOW_APPROVE) always does (BASITA-151). Both switches are off by default.
BASITA-205 (ADR-B001): `jira-md <KEY>` writes the issue as Markdown to <DIR>/work/jira/<KEY>.md and prints only that
path, followed by " (unchanged)" when the file already had the issue's latest `updated` (then it makes one small read
and writes nothing). DIR is --out, else BASITA_OPS_DIR, else ~/Code/basita-ops; a folder inside the app repository
(origin elbayoumy-farm-nutrition-platform) is refused. The file holds the fields, the description, links, sub-tasks
and the last 10 comments; Jira stays the master. The first time, it posts one comment on the issue with the file's
link, and never a second one. `jira-get <KEY>` refreshes that file the same way and prints it, so every agent read
goes through the snapshot; `jira-get <KEY> --json` prints the JSON below instead, reads only, and writes nothing.
The other read commands print JSON and change nothing: `jira-get --json` gives an issue with its comments, sub-tasks
and linked issues; `jira-jql` runs a JQL query; `confluence-get` gives a page with its body, comments, attachments and
children (with --storage, also the body in storage format, so a table can be edited and written back exactly with
`confluence-edit --storage`, BASITA-191); `confluence-search` finds pages by words or CQL. Each result names the agent
whose token answered (`read_by`). `confluence-create` and `confluence-edit` take plain text, or Confluence storage
format with --storage. `confluence-edit` replaces the body; Confluence keeps every earlier version. Nothing here
deletes.
BASITA-188: `confluence-attach <page> <file>` uploads a file (up to 25 MB) under its base name, or adds a new version
when the page already has an attachment of that name, and prints the name to use in `<ri:attachment ri:filename>`
and a ready `<ac:image>` line. `--label L` (repeatable) on `confluence-create` and `confluence-edit` adds labels
(lowercase, no spaces) and never removes any; `confluence-edit` with only labels makes no new page version.
`confluence-edit --parent <page id>` moves the page under another page of the same space, keeping its id (another
space, the page itself or its own descendants are refused before any write); with no text it keeps the body.
`jira-link` maps Relates, Blocks, Duplicate and Cloners (and their phrases) without reading the link-type list.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .client import (
    CLOSE_REASONS,
    ISSUE_KEY,
    AtlClient,
    AtlError,
    LinkCommentError,
    account_for,
    default_cloud_id,
    github_prs,
    keychain_token,
)

READS = {
    "jira-get": lambda c: c.jira_get,
    "jira-jql": lambda c: c.jira_jql,
    "confluence-get": lambda c: c.confluence_get,
    "confluence-search": lambda c: c.confluence_search,
}


def main(argv: list[str] | None = None, token_source=keychain_token, transport=None, pr_source=github_prs) -> int:
    parser = argparse.ArgumentParser(prog="atl", description="Read and write Jira and Confluence as a named agent.")
    parser.add_argument("agent", help="agent name, e.g. salma")
    parser.add_argument(
        "command",
        choices=[
            "jira-get",
            "jira-md",
            "jira-jql",
            "jira-comment",
            "jira-start",
            "jira-done",
            "jira-ready",
            "jira-flow",
            "jira-create",
            "jira-return",
            "jira-dismiss",
            "jira-edit",
            "jira-link",
            "confluence-get",
            "confluence-search",
            "confluence-create",
            "confluence-edit",
            "confluence-attach",
            "confluence-append",
        ],
    )
    parser.add_argument("target", help="issue key, JQL query, page id or URL, space key or search words, by command")
    parser.add_argument("text", nargs="?", default="", help="text to write, or - to read it from stdin")
    parser.add_argument(
        "why", nargs="?", default="", help="jira-flow: why, posted as the comment; jira-create: summary"
    )
    parser.add_argument("description", nargs="?", default="", help="jira-create: the description, or - for stdin")
    parser.add_argument("--mention", action="append", default=[], help="account id to @mention (repeatable)")
    parser.add_argument("--reason", choices=CLOSE_REASONS, help="jira-dismiss: why the issue is closed")
    parser.add_argument("--title", default="", help="confluence-create, confluence-edit: the page title")
    parser.add_argument(
        "--parent",
        default="",
        help="confluence-create: the parent page's id or URL; confluence-edit: move the page under this page "
        "(same space); jira-create: the parent issue",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="jira-create, confluence-create, confluence-edit: a label to add (repeatable; Confluence never removes)",
    )
    parser.add_argument("--assignee", default="", help="jira-create, jira-ready: who gets the issue")
    parser.add_argument("--link", default="", help="jira-create: an issue this one relates to")
    parser.add_argument("--storage", action="store_true", help="the text is Confluence storage format")
    parser.add_argument("--add-label", action="append", default=[], help="jira-edit: add a label (repeatable)")
    parser.add_argument("--remove-label", action="append", default=[], help="jira-edit: remove a label (repeatable)")
    parser.add_argument(
        "--set-labels", default=None, help='jira-edit: the whole label list, comma-separated ("" empties)'
    )
    parser.add_argument("--description", dest="new_description", default="", help="jira-edit: the new description")
    parser.add_argument("--priority", default="", help="jira-edit: Highest, High, Medium, Low or Lowest")
    parser.add_argument("--fix-version", action="append", default=[], help="jira-edit: add a Fix Version (repeatable)")
    parser.add_argument("--remove-fix-version", action="append", default=[], help="jira-edit: remove a Fix Version")
    parser.add_argument(
        "--set-fix-versions", default=None, help="jira-edit: the whole Fix Version list, comma-separated"
    )
    parser.add_argument("--summary", default="", help="jira-edit: the new summary")
    parser.add_argument("--due-date", default="", help="jira-edit: YYYY-MM-DD, or none")
    parser.add_argument("--type", dest="link_type", default="", help="jira-link: the link type or phrase (relates to)")
    parser.add_argument("--why", dest="done_why", default="", help="jira-done: what was delivered, or - for stdin")
    parser.add_argument(
        "--out",
        default="",
        help="jira-md, jira-get: the folder that holds work/jira/ (default BASITA_OPS_DIR, else ~/Code/basita-ops)",
    )
    parser.add_argument("--json", action="store_true", help="jira-get: print the issue as JSON, and write nothing")
    parser.add_argument("--via", default="", help="the agent whose Atlassian user to work through, e.g. sherif")
    parser.add_argument("--cloud-id", default=default_cloud_id())
    parser.add_argument("--domain", default=os.environ.get("BASITA_AGENT_DOMAIN", "basitasolutions.com"))
    args = parser.parse_args(argv)
    edit_options = (
        args.add_label,
        args.remove_label,
        args.set_labels is not None,
        args.new_description,
        args.priority,
        args.fix_version,
        args.remove_fix_version,
        args.set_fix_versions is not None,
        args.summary,
        args.due_date,
    )
    if args.command != "jira-edit" and any(edit_options):
        print(f"atl: those options are for jira-edit, not {args.command}", file=sys.stderr)
        return 2
    if args.command != "jira-link" and args.link_type:
        print(f"atl: --type is for jira-link, not {args.command}", file=sys.stderr)
        return 2
    if args.label and args.command not in ("jira-create", "jira-edit", "confluence-create", "confluence-edit"):
        print(
            f"atl: --label is for jira-create, confluence-create and confluence-edit, not {args.command}",
            file=sys.stderr,
        )
        return 2
    if args.command != "jira-done" and args.done_why:
        print(f"atl: --why is for jira-done, not {args.command}", file=sys.stderr)
        return 2
    if args.json and args.command != "jira-get":
        print(f"atl: --json is for jira-get, not {args.command}", file=sys.stderr)
        return 2
    if args.out and (args.command not in ("jira-md", "jira-get") or args.json):
        print("atl: --out is for jira-md and jira-get (without --json)", file=sys.stderr)
        return 2
    if args.command == "jira-md" or (args.command == "jira-get" and not args.json):
        return _snapshot(args, token_source, transport)
    if args.command in ("jira-edit", "jira-link"):
        return _edit_or_link(args, token_source, transport)
    if args.command == "jira-done":
        return _done(args, token_source, transport, pr_source)

    if args.command in READS:
        if args.storage and args.command != "confluence-get":
            print(f"atl: --storage is for confluence-get, create and edit, not {args.command}", file=sys.stderr)
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
            if args.command == "confluence-get" and args.storage:  # BASITA-191: the body as storage, to edit it exactly
                result = client.confluence_get(args.target, storage=True)
            else:
                result = READS[args.command](client)(args.target)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except AtlError as error:
            print(f"atl: {error}", file=sys.stderr)
            return 1
        return 0

    if args.command == "jira-create":
        description = sys.stdin.read() if args.description == "-" else args.description
        if not (args.text and args.why.strip() and description.strip()):
            print('atl: jira-create needs <PROJECT> <type> "summary" "description"', file=sys.stderr)
            return 2
        try:
            agent = args.agent.lower()
            kwargs = {"transport": transport} if transport else {}
            client = AtlClient(
                agent,
                args.cloud_id,
                token_source(account_for(agent, args.via), "jira"),
                args.domain,
                via=args.via,
                **kwargs,
            )
            signed = f"{description.rstrip()}\n\n— {client.signature}"
            if args.link and not ISSUE_KEY.match(args.link):
                raise AtlError(f"'{args.link}' is not an issue key")
            key = client.jira_create(
                args.target, args.text, args.why, signed, args.parent, tuple(args.label), args.assignee, args.link
            )
            assignee = client.create_assignee(args.target, args.text, args.assignee)
            to = f"assigned to {assignee.capitalize()}" if assignee else "unassigned"
            print(f"{key} created, {to}, by {client.reader}")
            if args.link:
                try:
                    client.jira_link(key, args.link)
                except AtlError as error:
                    raise AtlError(f"{key} was created, but not linked to {args.link}: {error}") from error
                print(f"{key} relates to {args.link}")
        except AtlError as error:
            print(f"atl: {error}", file=sys.stderr)
            return 1
        return 0
    if args.description or (args.why and args.command != "jira-flow"):
        print(f"atl: {args.command} takes one text", file=sys.stderr)
        return 2
    flow_action = args.text if args.command == "jira-flow" else ""
    text = args.why if args.command == "jira-flow" else args.text
    if args.command == "confluence-attach":
        return _attach(args, token_source, transport)
    text = sys.stdin.read() if text == "-" else text
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
                result = client.confluence_create(
                    args.target, args.title.strip(), text, args.parent, args.storage, tuple(args.label)
                )
            else:
                result = client.confluence_edit(
                    args.target, text, args.title, args.storage, parent=args.parent, labels=tuple(args.label)
                )
            links = result.get("_links") or {}
            url = (links.get("base") or "") + (links.get("webui") or "")
            version = (result.get("version") or {}).get("number")
            if version or not result.get("labels_added"):  # labels alone make no new version
                print(f"page {result.get('id')} version {version} by {client.display} {url}".rstrip())
            if result.get("labels_added"):
                print(f"page {result.get('id')} labels added: {', '.join(result['labels_added'])}")
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
        print("atl: empty text (jira-return and jira-flow need the reason)", file=sys.stderr)
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
            status = client.jira_ready(args.target, args.assignee)
            client.jira_comment(args.target, signed)
            to = f", assigned to {args.assignee.capitalize()}" if args.assignee else ""
            print(f"{args.target} -> {status}{to}, by {client.display}")
        elif args.command == "jira-flow":
            status = client.jira_flow(args.target, flow_action)
            client.jira_comment(args.target, f"{flow_action}: moved to {status}. {signed}")
            print(f"{args.target} -> {status}, {flow_action}, by {client.display}")
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


def _split(value: str | None) -> tuple | None:
    """A comma-separated list from an option: None when the option is absent, () for an empty string."""
    if value is None:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _snapshot(args, token_source, transport) -> int:
    """jira-md prints the snapshot's path; jira-get prints the snapshot itself (BASITA-205)."""
    others = (args.mention, args.reason, args.title, args.parent, args.label, args.assignee, args.link, args.storage)
    if args.text or args.why or args.description or any(others):
        print(f"atl: {args.command} takes the issue key, and --out DIR only", file=sys.stderr)
        return 2
    out = args.out or os.environ.get("BASITA_OPS_DIR") or os.path.join(os.path.expanduser("~"), "Code", "basita-ops")
    try:
        agent = args.agent.lower()
        kwargs = {"transport": transport} if transport else {}
        client = AtlClient(
            agent,
            args.cloud_id,
            token_source(account_for(agent, args.via), "jira"),
            args.domain,
            via=args.via,
            **kwargs,
        )
        if args.command == "jira-md":
            result = client.jira_md(args.target, out)
            print(result["path"] + ("" if result["changed"] else " (unchanged)"))
            return 0
        # jira-get: reading never fails for want of the folder or of the link comment.
        if not os.path.isdir(os.path.expanduser(out)):
            sys.stdout.write(client.jira_md_text(args.target))
            print("jira-get: no basita-ops folder here; printed the ticket without saving a snapshot", file=sys.stderr)
            return 0
        try:
            path = client.jira_md(args.target, out)["path"]
        except LinkCommentError as error:
            print(f"jira-get: {error}", file=sys.stderr)
            path = error.path
        with open(path, encoding="utf-8") as snapshot:
            sys.stdout.write(snapshot.read())
    except (AtlError, OSError) as error:
        print(f"atl: {error}", file=sys.stderr)
        return 1
    return 0


def _attach(args, token_source, transport) -> int:
    others = (args.mention, args.title, args.parent, args.label, args.assignee, args.link, args.storage)
    if not args.text or args.why or args.description or any(others):
        print("atl: confluence-attach takes a page and a file: confluence-attach <page> <file>", file=sys.stderr)
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
        result = client.confluence_attach(args.target, args.text)
        shown = f"{result['name']} (id {result['id']}, version {result['version']})"
        print(f"attachment {shown} on page {args.target}, by {client.display}")
        print(f"use: {result['ref']}")
    except AtlError as error:
        print(f"atl: {error}", file=sys.stderr)
        return 1
    return 0


def _done(args, token_source, transport, pr_source) -> int:
    others = (args.mention, args.reason, args.title, args.parent, args.label, args.assignee, args.link, args.storage)
    if args.text or args.why or args.description or any(others):
        print('atl: jira-done takes the issue key and --why "what was delivered" only', file=sys.stderr)
        return 2
    why = sys.stdin.read() if args.done_why == "-" else args.done_why
    if not why.strip():
        print('atl: jira-done needs --why "what was delivered"', file=sys.stderr)
        return 2
    try:
        agent = args.agent.lower()
        kwargs = {"transport": transport} if transport else {}
        client = AtlClient(
            agent,
            args.cloud_id,
            token_source(account_for(agent, args.via), "jira"),
            args.domain,
            via=args.via,
            **kwargs,
        )
        status, merged = client.jira_done(args.target, why, pr_source)
        prs = f", {len(merged)} merged PR(s)" if merged else ", no PR"
        print(f"{args.target} -> {status}{prs}, by {client.display}")
    except AtlError as error:
        print(f"atl: {error}", file=sys.stderr)
        return 1
    return 0


def _edit_or_link(args, token_source, transport) -> int:
    if args.command == "jira-edit":
        if args.text or args.why or args.description:
            print("atl: jira-edit takes the issue key and options only", file=sys.stderr)
            return 2
        if args.label or args.link:
            print("atl: jira-edit takes --add-label, --remove-label or --set-labels, and no --link", file=sys.stderr)
            return 2
    elif not args.text or args.why or args.description:
        print('atl: jira-link needs two issue keys: jira-link <KEY> <KEY> [--type "blocks"]', file=sys.stderr)
        return 2
    try:
        agent = args.agent.lower()
        kwargs = {"transport": transport} if transport else {}
        client = AtlClient(
            agent,
            args.cloud_id,
            token_source(account_for(agent, args.via), "jira"),
            args.domain,
            via=args.via,
            **kwargs,
        )
        if args.command == "jira-link":
            relation = client.jira_link_typed(args.target, args.text, args.link_type or "relates to")
            print(f"{args.target} {relation} {args.text}, by {client.display}")
            return 0
        description = sys.stdin.read() if args.new_description == "-" else args.new_description
        if args.new_description and not description.strip():
            raise AtlError("the description is empty")
        signed = f"{description.rstrip()}\n\n— {client.signature}" if description.strip() else ""
        changes = client.jira_edit(
            args.target,
            add_labels=tuple(args.add_label),
            remove_labels=tuple(args.remove_label),
            set_labels=_split(args.set_labels),
            assignee=args.assignee,
            description=signed,
            priority=args.priority,
            add_fix_versions=tuple(args.fix_version),
            remove_fix_versions=tuple(args.remove_fix_version),
            set_fix_versions=_split(args.set_fix_versions),
            summary=args.summary,
            parent=args.parent,
            duedate=args.due_date,
        )
        print(f"{args.target} edited by {client.display}: {'; '.join(changes)}")
    except AtlError as error:
        print(f"atl: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
