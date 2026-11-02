"""What an agent may run with its own GitHub identity, and which token each command gets.

Two controls work together. The token is the real one: a read gets a read-only token, a pull request
write gets pull_requests:write with contents:read (which cannot merge or push), and only `git push`
gets contents:write. On top of that, this module refuses what only Ahmed does: merging, pushes or ref
updates to main, deletes, settings, secrets, and the labels only he sets. It is a guard, not a seal:
an agent on the same Mac can reach the key another way, and review is what keeps that honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .identity import PR_WRITE, PUSH, READ, IdentityError

GITHUB_URL = re.compile(r"^https://github\.com/([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?(?:/|$)")
REMOTE = re.compile(
    r"^(?:https://(?:[^@/:]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$"
)

# gh subcommands, by the token they need. Anything not listed is refused.
READS = {
    "pr": {"view", "list", "diff", "checks", "status"},
    "issue": {"view", "list", "status"},
    "repo": {"view"},
    "run": {"view", "list"},
    "release": {"view", "list"},
    "label": {"list"},
    "search": {"prs", "issues", "code", "commits", "repos"},
}
WRITES = {
    "pr": {"create", "edit", "comment", "review", "ready"},
    "issue": {"comment"},  # the Apps have no issues permission, so this works on pull requests only
}
# A user agent (Yousef) reviews: reads, reviews and comments only.
USER_WRITES = {"pr": {"review", "comment"}, "issue": {"comment"}}
WHY = {
    ("pr", "merge"): "merging is Ahmed's",
    ("pr", "close"): "closing a pull request is Ahmed's",
    ("pr", "reopen"): "reopening a pull request is Ahmed's",
    ("auth", ""): "agent-gh never touches gh's login",
    ("secret", ""): "secrets are Ahmed's",
    ("variable", ""): "variables are Ahmed's",
    ("ruleset", ""): "rulesets are Ahmed's",
    ("workflow", ""): "workflows are Ahmed's (D4)",
    ("cache", ""): "caches are Ahmed's",
}
# Flags that take a value, per command, so a value isn't mistaken for a subcommand or a repo.
VALUE_FLAGS = {
    "-R",
    "--repo",
    "-t",
    "--title",
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-B",
    "--base",
    "-H",
    "--head",
    "-l",
    "--label",
    "-a",
    "--assignee",
    "-r",
    "--reviewer",
    "-m",
    "--milestone",
    "-p",
    "--project",
    "--add-label",
    "--remove-label",
    "--add-reviewer",
    "--remove-reviewer",
    "--add-assignee",
    "--remove-assignee",
    "--add-project",
    "--remove-project",
    "-q",
    "--jq",
    "--template",
    "-S",
    "--search",
    "-s",
    "--state",
    "-L",
    "--limit",
    "-A",
    "--author",
    "--json",
    "--hostname",
    "--repository",
    "-O",
    "--output",
    "--job",
    "-j",
}
LABEL_FLAGS = {"-l", "--label", "--add-label", "--remove-label"}
API_VALUE_FLAGS = {
    "-X",
    "--method",
    "-H",
    "--header",
    "-f",
    "--raw-field",
    "-F",
    "--field",
    "--input",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--hostname",
    "--cache",
    "-p",
    "--preview",
}
API_FIELD_FLAGS = {"-f", "--raw-field", "-F", "--field"}
# Write calls an agent may make through `gh api`, and whether a user agent (Yousef) may too.
# Everything else that writes is refused.
API_WRITES = [
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/comments$"), True),
    ("PATCH", re.compile(r"^repos/[^/]+/[^/]+/issues/comments/\d+$"), True),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/labels$"), False),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls$"), False),
    ("PATCH", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+$"), False),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/comments$"), True),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/comments/\d+/replies$"), True),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews$"), True),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/requested_reviewers$"), False),
]
# Fields whose value must be visible to be checked, so gh may not read them from a file (key=@file).
CHECKED_FIELDS = ("labels", "state", "base", "event")
PUSH_FLAGS = {
    "-u",
    "--set-upstream",
    "-n",
    "--dry-run",
    "-q",
    "--quiet",
    "-v",
    "--verbose",
    "--porcelain",
    "--progress",
    "--no-progress",
    "--no-verify",
    "--atomic",
}
PUSH_REFUSED = {
    "-f": "forced",
    "--force": "forced",
    "--force-with-lease": "forced",
    "--force-if-includes": "forced",
    "-d": "deleting",
    "--delete": "deleting",
    "--prune": "deleting",
    "--mirror": "mirror",
    "--all": "bulk",
    "--branches": "bulk",
    "--tags": "tag",
    "--follow-tags": "tag",
}


@dataclass
class Plan:
    """What to run: the repository the token is for, its permissions, and the arguments."""

    repo: str
    permissions: dict
    args: list[str] = field(default_factory=list)


def _refuse(why: str) -> IdentityError:
    return IdentityError(f"refused: {why}")


def _flag_values(args: list[str], names: set[str]) -> list[str]:
    """Values of the flags in `names`, in any of gh's spellings (--x v, --x=v, -x v, -xv)."""
    values = []
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, value = arg.partition("=")
        if eq and name in names:
            values.append(value)
        elif arg in names and i + 1 < len(args):
            values.append(args[i + 1])
            i += 1
        elif len(arg) > 2 and arg[0] == "-" and arg[1] != "-" and arg[:2] in names:
            values.append(arg[2:])
        i += 1
    return values


def _positionals(args: list[str], value_flags: set[str]) -> list[str]:
    found = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            found.extend(args[i + 1 :])
            break
        if arg.startswith("-"):
            if arg in value_flags:
                i += 1
        else:
            found.append(arg)
        i += 1
    return found


def _check_labels(values: list[str], ahmed_only: list[str]) -> None:
    protected = {label.lower() for label in ahmed_only}
    for value in values:
        for label in re.split(r"[,\s]+", value.lower()):
            if label.strip("\"'") in protected:
                raise _refuse(f"the label {label} is set only by Ahmed")


def _approves(args: list[str]) -> bool:
    """--approve in any spelling, including --approve=true and a short cluster such as -ab."""
    return any(a.startswith("--approve") or (re.fullmatch(r"-[A-Za-z]+", a) and "a" in a[1:]) for a in args)


def repo_from_remote(url: str) -> str:
    match = REMOTE.match(url.strip())
    if not match:
        raise IdentityError(f"the remote {url.strip()} is not a github.com repository")
    return match.group(1)


def plan_gh(agent: str, entry: dict, args: list[str], default_repo: str | None, config: dict) -> Plan:
    """Refuse or plan one `gh` command."""
    if not args:
        raise IdentityError("usage: agent-gh <agent> <gh arguments...>")
    hosts = _flag_values(args, {"--hostname"})
    if any(h.lower() != "github.com" for h in hosts):
        raise _refuse("only github.com")
    command = args[0]
    rest = args[1:]
    sub = rest[0] if rest and not rest[0].startswith("-") else ""
    for key in ((command, sub), (command, "")):
        if key in WHY:
            raise _refuse(WHY[key])
    if command == "api":
        return _plan_api(agent, entry, rest, default_repo, config)
    if "--delete-branch" in rest:
        raise _refuse("deleting a branch")
    writes = USER_WRITES if entry["kind"] == "user" else WRITES
    if sub in writes.get(command, set()):
        permissions = PR_WRITE
    elif sub in READS.get(command, set()):
        permissions = READ
    else:
        raise _refuse(f"`gh {command} {sub}`".replace(" `", "`") + " is not on agent-gh's list")
    _check_labels(_flag_values(rest, LABEL_FLAGS), config.get("ahmed_only_labels", []))
    if command == "pr" and sub == "review" and entry["kind"] == "app" and _approves(rest[1:]):
        raise _refuse("an App does not approve pull requests; Yousef reviews")
    if command == "pr" and sub == "edit" and _flag_values(rest, {"-B", "--base"}):
        raise _refuse("retargeting a pull request; open a new one instead")
    repo = _repo_of(rest[1:], default_repo)
    return Plan(repo, permissions, args)


def _repo_of(rest: list[str], default_repo: str | None) -> str:
    named = _flag_values(rest, {"-R", "--repo"})
    urls = {m.group(1) for a in _positionals(rest, VALUE_FLAGS) for m in [GITHUB_URL.match(a)] if m}
    candidates = {r.lower() for r in named} | {u.lower() for u in urls}
    if len(candidates) > 1:
        raise _refuse(f"the command names more than one repository ({', '.join(sorted(candidates))})")
    if named:
        return named[0]
    if urls:
        return urls.pop()
    if default_repo:
        return default_repo
    raise IdentityError("no repository: pass -R owner/name or run inside a clone of a github.com repository")


def _plan_api(agent: str, entry: dict, rest: list[str], default_repo: str | None, config: dict) -> Plan:
    positionals = _positionals(rest, API_VALUE_FLAGS)
    if not positionals:
        raise IdentityError("usage: agent-gh <agent> api <path> [flags]")
    path = positionals[0].lstrip("/").split("?")[0]
    methods = _flag_values(rest, {"-X", "--method"})
    fields = _flag_values(rest, API_FIELD_FLAGS)
    has_body = bool(fields) or bool(_flag_values(rest, {"--input"}))
    method = (methods[-1] if methods else ("POST" if has_body else "GET")).upper()
    if path == "graphql":
        if any(re.search(r"\bmutation\b", value) for value in fields):
            raise _refuse("GraphQL mutations; use the gh command instead")
        repo = default_repo or ""
        if not repo:
            raise IdentityError("no repository for the token: run inside a clone or use a REST path")
        return Plan(repo, READ, ["api", *rest])
    placeholder = path.startswith("repos/{owner}/{repo}")
    match = re.match(r"^repos/([^/]+)/([^/]+)", path)
    if placeholder:
        if not default_repo:
            raise IdentityError("{owner}/{repo} needs a clone of a github.com repository")
        repo = default_repo
        path = f"repos/{repo}" + path[len("repos/{owner}/{repo}") :]
    elif match:
        repo = f"{match.group(1)}/{match.group(2)}"
    elif method == "GET" and default_repo:
        repo = default_repo
    else:
        raise _refuse(f"{method} {path} is outside a repository")
    if method == "GET":
        return Plan(repo, READ, ["api", *rest])
    if method == "DELETE":
        raise _refuse("deleting on GitHub")
    user = entry["kind"] == "user"
    if not any(method == m and pattern.match(path) and (ok or not user) for m, pattern, ok in API_WRITES):
        raise _refuse(f"{method} {path} is not on agent-gh's list of writes for {agent}")
    if any(f.split("=", 1)[0].startswith(CHECKED_FIELDS) and f.partition("=")[2].startswith("@") for f in fields):
        raise _refuse("a labels, state, base or event field read from a file; pass the value itself")
    if re.search(r"/pulls/\d+$", path) and any(re.match(r"^(state|base)=", f) for f in fields):
        raise _refuse("closing or retargeting a pull request through the API")
    if path.endswith("/reviews") and entry["kind"] == "app" and any(f.upper() == "EVENT=APPROVE" for f in fields):
        raise _refuse("an App does not approve pull requests; Yousef reviews")
    if _flag_values(rest, {"--input"}):
        raise _refuse("--input on a write; pass the fields with -f so they can be checked")
    _check_labels([f.partition("=")[2] for f in fields if f.startswith("labels")], config.get("ahmed_only_labels", []))
    return Plan(repo, PR_WRITE, ["api", *rest])


def plan_push(args: list[str], current_branch: str | None, remote_url: str, config: dict) -> Plan:
    """Refuse or plan one `git push`. The refspecs are made explicit, so the destination is known."""
    protected = {b.lower() for b in config.get("protected_branches", ["main", "master"])}
    flags, positionals = [], []
    for arg in args:
        if arg.startswith("-"):
            name = arg.split("=", 1)[0]
            if name in PUSH_REFUSED:
                raise _refuse(f"a {PUSH_REFUSED[name]} push")
            if name not in PUSH_FLAGS:
                raise _refuse(f"the push option {name} is not on agent-git's list")
            flags.append(arg)
        else:
            positionals.append(arg)
    remote = positionals[0] if positionals else "origin"
    refspecs = positionals[1:]
    if not refspecs:
        if not current_branch:
            raise _refuse("pushing a detached HEAD; name the branch")
        refspecs = [f"HEAD:refs/heads/{current_branch}"]
    planned = []
    for spec in refspecs:
        if spec.startswith("+"):
            raise _refuse("a forced push (+refspec)")
        if "*" in spec:
            raise _refuse("a wildcard refspec")
        src, colon, dst = spec.partition(":")
        if colon and not src:
            raise _refuse("deleting a remote branch")
        if not colon:
            dst = current_branch if src == "HEAD" and current_branch else src
        if src == "HEAD" and not colon and not current_branch:
            raise _refuse("pushing a detached HEAD; name the branch")
        if dst.startswith("refs/") and not dst.startswith("refs/heads/"):
            raise _refuse(f"updating {dst}; agents push branches only")
        name = dst[len("refs/heads/") :] if dst.startswith("refs/heads/") else dst
        if name.lower() in protected:
            raise _refuse(f"pushing to {name}; only Ahmed updates it, by merging")
        if not name:
            raise _refuse("an empty destination")
        planned.append(f"{src}:refs/heads/{name}")
    return Plan(repo_from_remote(remote_url), PUSH, [*flags, remote, *planned])
