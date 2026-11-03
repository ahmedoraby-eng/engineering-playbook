"""REST stand-ins for the gh commands that speak GraphQL, for a cloud session (plan 10.8, BASITA-163).

Anthropic's GitHub proxy blocks GraphQL whatever token is used, and `gh pr ...` speaks GraphQL. So in
REST mode (cloud.rest_mode) agent-gh runs these four itself, through the REST API, with the token the
policy planned for the original command:

    pr create   POST /repos/{r}/pulls, then labels and reviewers
    pr view     GET  /repos/{r}/pulls/{n}  (text like gh's, or --json with the fields listed below)
    pr comment  POST /repos/{r}/issues/{n}/comments
    pr checks   GET  check runs and the combined status of the head commit (exit 1 on a failure, 8 pending)

The policy (policy.plan_gh) has already approved the original gh command, so labels only Ahmed sets,
merging and the rest are refused before anything here runs, and nothing here asks for more than the
plan's permissions. A flag these stand-ins don't know is refused rather than dropped. `gh api` on a REST
path runs gh itself; GraphQL and every other gh command are refused with the way round it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import urllib.parse
from typing import TYPE_CHECKING

from .identity import IdentityError, api, revoke_installation_token, urllib_transport

if TYPE_CHECKING:
    from collections.abc import Callable

    from .identity import Transport
    from .policy import Plan

PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/pull/(\d+)(?:[/?#].*)?$")
REPO_FLAGS = {"-R": "repo", "--repo": "repo"}
CREATE_VALUES = {
    **REPO_FLAGS,
    "-t": "title",
    "--title": "title",
    "-b": "body",
    "--body": "body",
    "-F": "body-file",
    "--body-file": "body-file",
    "-B": "base",
    "--base": "base",
    "-H": "head",
    "--head": "head",
    "-l": "label",
    "--label": "label",
    "-r": "reviewer",
    "--reviewer": "reviewer",
}
CREATE_BOOLS = {"-d": "draft", "--draft": "draft"}
VIEW_VALUES = {**REPO_FLAGS, "--json": "json"}
COMMENT_VALUES = {**REPO_FLAGS, "-b": "body", "--body": "body", "-F": "body-file", "--body-file": "body-file"}
CHECKS_VALUES = dict(REPO_FLAGS)


def _state(pr: dict) -> str:
    if pr.get("merged_at"):
        return "MERGED"
    if pr.get("state") == "closed":
        return "CLOSED"
    return "OPEN"


def _mergeable(pr: dict) -> str:
    return {True: "MERGEABLE", False: "CONFLICTING"}.get(pr.get("mergeable"), "UNKNOWN")


# gh's --json field names, from the REST pull request.
JSON_FIELDS: dict[str, Callable[[dict], object]] = {
    "number": lambda pr: pr.get("number"),
    "title": lambda pr: pr.get("title"),
    "body": lambda pr: pr.get("body") or "",
    "state": _state,
    "url": lambda pr: pr.get("html_url"),
    "author": lambda pr: {"login": (pr.get("user") or {}).get("login")},
    "headRefName": lambda pr: (pr.get("head") or {}).get("ref"),
    "headRefOid": lambda pr: (pr.get("head") or {}).get("sha"),
    "baseRefName": lambda pr: (pr.get("base") or {}).get("ref"),
    "isDraft": lambda pr: bool(pr.get("draft")),
    "closed": lambda pr: pr.get("state") == "closed",
    "labels": lambda pr: [{"name": label.get("name")} for label in pr.get("labels") or []],
    "mergeable": _mergeable,
    "mergedAt": lambda pr: pr.get("merged_at"),
    "createdAt": lambda pr: pr.get("created_at"),
    "updatedAt": lambda pr: pr.get("updated_at"),
    "additions": lambda pr: pr.get("additions"),
    "deletions": lambda pr: pr.get("deletions"),
    "changedFiles": lambda pr: pr.get("changed_files"),
}


def graphql_refusal(agent: str, what: str) -> IdentityError:
    return IdentityError(
        f"refused: GraphQL is blocked in the cloud, and {what} has no REST mapping in agent-gh yet; "
        f"use `agent-gh {agent} api …` with a REST path"
    )


def _subcommand(args: list[str]) -> tuple[str, str]:
    command = args[0] if args else ""
    rest = args[1:]
    return command, rest[0] if rest and not rest[0].startswith("-") else ""


def parse(agent: str, what: str, args: list[str], values: dict, bools: dict | None = None) -> tuple[dict, list]:
    """gh's flags in their spellings (--x v, --x=v, -x v, -xv). Returns ({name: [values]}, positionals).
    A flag not in `values` or `bools` is refused, so nothing an agent asked for is silently dropped."""
    bools = bools or {}
    options: dict = {}
    positionals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals.extend(args[i + 1 :])
            break
        if arg.startswith("-") and len(arg) > 1:
            if arg.startswith("--"):
                name, eq, value = arg.partition("=")
                inline = value if eq else None
            else:
                name, inline = arg[:2], (arg[2:] or None)
            if name in values:
                if inline is None:
                    if i + 1 >= len(args):
                        raise IdentityError(f"{name} needs a value")
                    i += 1
                    inline = args[i]
                options.setdefault(values[name], []).append(inline)
            elif name in bools and inline is None:
                options[bools[name]] = [True]
            else:
                raise graphql_refusal(agent, f"`gh {what} {name}`")
        else:
            positionals.append(arg)
        i += 1
    return options, positionals


def _one(options: dict, name: str) -> str | None:
    found = options.get(name) or []
    if len(found) > 1:
        raise IdentityError(f"--{name} given {len(found)} times")
    return found[0] if found else None


def _many(options: dict, name: str) -> list[str]:
    return [part.strip() for value in options.get(name) or [] for part in value.split(",") if part.strip()]


def _git_branch() -> str | None:
    result = subprocess.run(["git", "symbolic-ref", "--short", "-q", "HEAD"], capture_output=True, text=True)
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _body(options: dict, stdin) -> str:
    body, file = _one(options, "body"), _one(options, "body-file")
    if body is not None and file is not None:
        raise IdentityError("give --body or --body-file, not both")
    if file is None:
        return body or ""
    if file == "-":
        return stdin.read()
    try:
        with open(file, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise IdentityError(f"cannot read {file}: {error.strerror}") from None


class Rest:
    """One translated command: the plan, the token's Authorization, and the seams the tests replace."""

    def __init__(
        self,
        agent: str,
        plan: Plan,
        token: str,
        transport: Transport = urllib_transport,
        branch: Callable[[], str | None] = _git_branch,
        out=None,
        stdin=None,
    ):
        self.agent = agent
        self.plan = plan
        self.repo = plan.repo
        self.auth = f"token {token}"
        self.transport = transport
        self.branch = branch
        self.out = out or sys.stdout
        self.stdin = stdin or sys.stdin

    def call(self, method: str, path: str, body: dict | None = None):
        return api(self.transport, method, path, self.auth, body)[1]

    def say(self, text: str) -> None:
        self.out.write(text + "\n")

    def number(self, selector: str | None) -> int:
        """A pull request number from what gh accepts: a number, #number, its URL, a branch, or none."""
        if selector and re.fullmatch(r"#?\d+", selector):
            return int(selector.lstrip("#"))
        url = PR_URL.match(selector or "")
        if url:
            if url.group(1).lower() != self.repo.lower():
                raise IdentityError(f"refused: {selector} is not in {self.repo}")
            return int(url.group(2))
        branch = selector or self.branch()
        if not branch:
            raise IdentityError("no pull request given and no current branch")
        owner = self.repo.split("/")[0]
        head = urllib.parse.quote(f"{owner}:{branch}", safe=":/")
        found = self.call("GET", f"/repos/{self.repo}/pulls?head={head}&state=all&per_page=1")
        if not found:
            raise IdentityError(f"no pull request found for the branch {branch}")
        return int(found[0]["number"])

    def pr(self, selector: str | None) -> dict:
        return self.call("GET", f"/repos/{self.repo}/pulls/{self.number(selector)}")

    @staticmethod
    def selector(positionals: list[str]) -> str | None:
        if len(positionals) > 1:
            raise IdentityError(f"one pull request at a time (got {' '.join(positionals)})")
        return positionals[0] if positionals else None

    # --- the four commands -----------------------------------------------------------------------

    def create(self, args: list[str]) -> int:
        options, positionals = parse(self.agent, "pr create", args, CREATE_VALUES, CREATE_BOOLS)
        if positionals:
            raise IdentityError(f"gh pr create takes no arguments (got {' '.join(positionals)})")
        title = _one(options, "title")
        if not title:
            raise IdentityError("--title is needed in REST mode (no --fill)")
        text = _body(options, self.stdin)
        head = _one(options, "head") or self.branch()
        if not head:
            raise IdentityError("no --head and no current branch")
        base = _one(options, "base") or self.call("GET", f"/repos/{self.repo}").get("default_branch")
        body = {"title": title, "head": head, "base": base, "body": text}
        if options.get("draft"):
            body["draft"] = True
        pr = self.call("POST", f"/repos/{self.repo}/pulls", body)
        number = pr["number"]
        labels = _many(options, "label")
        if labels:
            self.call("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": labels})
        reviewers = _many(options, "reviewer")
        if reviewers:
            users = [r for r in reviewers if "/" not in r]
            teams = [r.split("/", 1)[1] for r in reviewers if "/" in r]
            self.call(
                "POST",
                f"/repos/{self.repo}/pulls/{number}/requested_reviewers",
                {"reviewers": users, "team_reviewers": teams},
            )
        self.say(pr.get("html_url") or f"https://github.com/{self.repo}/pull/{number}")
        return 0

    def view(self, args: list[str]) -> int:
        options, positionals = parse(self.agent, "pr view", args, VIEW_VALUES)
        fields = _many(options, "json")
        if options.get("json") is not None and not fields:
            raise IdentityError(f"--json needs fields: {', '.join(sorted(JSON_FIELDS))}")
        unknown = [f for f in fields if f not in JSON_FIELDS]
        if unknown:
            raise IdentityError(
                f"--json {', '.join(unknown)} has no REST mapping; the fields are {', '.join(sorted(JSON_FIELDS))}"
            )
        pr = self.pr(self.selector(positionals))
        if fields:
            self.say(json.dumps({f: JSON_FIELDS[f](pr) for f in fields}, separators=(",", ":")))
            return 0
        state = "DRAFT" if pr.get("draft") and _state(pr) == "OPEN" else _state(pr)
        labels = ", ".join(label.get("name", "") for label in pr.get("labels") or [])
        for name, value in (
            ("title", pr.get("title")),
            ("state", state),
            ("author", (pr.get("user") or {}).get("login")),
            ("labels", labels),
            ("number", pr.get("number")),
            ("url", pr.get("html_url")),
            ("base", (pr.get("base") or {}).get("ref")),
            ("head", (pr.get("head") or {}).get("ref")),
            ("additions", pr.get("additions")),
            ("deletions", pr.get("deletions")),
        ):
            self.say(f"{name}:\t{'' if value is None else value}")
        self.say("--")
        self.say(pr.get("body") or "")
        return 0

    def comment(self, args: list[str]) -> int:
        options, positionals = parse(self.agent, "pr comment", args, COMMENT_VALUES)
        text = _body(options, self.stdin)
        if not text.strip():
            raise IdentityError("--body or --body-file is needed in REST mode (no editor)")
        number = self.number(self.selector(positionals))
        made = self.call("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": text})
        self.say(made.get("html_url") or f"https://github.com/{self.repo}/pull/{number}")
        return 0

    def checks(self, args: list[str]) -> int:
        _, positionals = parse(self.agent, "pr checks", args, CHECKS_VALUES)
        pr = self.pr(self.selector(positionals))
        sha = (pr.get("head") or {}).get("sha")
        rows = []
        try:
            runs: dict = {}
            page = 1
            while True:
                answer = self.call("GET", f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100&page={page}")
                batch = answer.get("check_runs") or []
                for run in batch:  # a re-run leaves the older run behind: keep the latest of each name
                    if run.get("name") not in runs or run.get("id", 0) > runs[run["name"]].get("id", 0):
                        runs[run.get("name")] = run
                if len(batch) < 100:
                    break
                page += 1
            status = self.call("GET", f"/repos/{self.repo}/commits/{sha}/status")
        except IdentityError as error:
            if " 403 " in str(error):
                raise IdentityError(f"{error} (this App's token can't read checks or commit statuses here)") from None
            raise
        for run in runs.values():
            rows.append(
                (
                    run.get("name", ""),
                    _run_bucket(run),
                    _elapsed(run.get("started_at"), run.get("completed_at")),
                    run.get("html_url") or run.get("details_url") or "",
                    ((run.get("output") or {}).get("title") or ""),
                )
            )
        for context in status.get("statuses") or []:
            rows.append(
                (
                    context.get("context", ""),
                    _status_bucket(context.get("state")),
                    "0",
                    context.get("target_url") or "",
                    context.get("description") or "",
                )
            )
        if not rows:
            print(f"no checks reported on the '{(pr.get('head') or {}).get('ref')}' branch", file=sys.stderr)
            return 1
        for row in sorted(rows, key=lambda r: (r[1] != "fail", r[1] != "pending", r[0].lower())):
            self.say("\t".join(row))
        buckets = {row[1] for row in rows}
        return 1 if "fail" in buckets else 8 if "pending" in buckets else 0


def _run_bucket(run: dict) -> str:
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "pass"
    if conclusion in ("neutral", "skipped"):
        return "skipping"
    if conclusion == "cancelled":
        return "cancel"
    return "fail"


def _status_bucket(state: str | None) -> str:
    return {"success": "pass", "pending": "pending"}.get(state or "", "fail")


def _elapsed(started: str | None, completed: str | None) -> str:
    if not started or not completed:
        return "0"
    try:
        start = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return "0"
    seconds = max(0, int((end - start).total_seconds()))
    return f"{seconds // 60}m{seconds % 60}s" if seconds >= 60 else f"{seconds}s"


COMMANDS = {
    ("pr", "create"): Rest.create,
    ("pr", "view"): Rest.view,
    ("pr", "comment"): Rest.comment,
    ("pr", "checks"): Rest.checks,
}


def runner(agent: str, plan: Plan) -> Callable[..., int] | None:
    """What runs the planned command in REST mode: None when gh itself may (a REST `gh api` call), a
    function for the four stand-ins, and a refusal for anything that would need GraphQL. Called after
    the policy approved the command and before a token is minted."""
    command, sub = _subcommand(plan.args)
    if command == "api":
        if any(a.lstrip("/").split("?")[0] == "graphql" for a in plan.args[1:] if not a.startswith("-")):
            raise IdentityError("refused: GraphQL is blocked in the cloud; use a REST path with agent-gh api")
        return None
    handler = COMMANDS.get((command, sub))
    if handler is None:
        raise graphql_refusal(agent, f"`gh {command} {sub}`".replace(" `", "`"))
    rest_args = plan.args[2:]

    def run(token: str, transport: Transport = urllib_transport, **seams) -> int:
        return handler(Rest(agent, plan, token, transport, **seams), rest_args)

    return run


def run_and_revoke(
    run: Callable[..., int], token: str, revocable: bool, transport: Transport = urllib_transport
) -> int:
    """Runs one stand-in, then revokes an App token at once, even when it failed."""
    try:
        return run(token, transport)
    finally:
        if revocable and not revoke_installation_token(token, transport):
            print("agent-gh: could not revoke the token; it expires within the hour", file=sys.stderr)
