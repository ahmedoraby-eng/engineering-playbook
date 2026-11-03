"""agent-gh, agent-git push, gh-check and gh-store, as one agent's GitHub identity.

    python -m agent_gh gh <agent> <gh arguments...>      (scripts/agent-gh)
    python -m agent_gh push <agent> <git push arguments...>  (scripts/agent-git <agent> push)
    python -m agent_gh check [agent...]                   (scripts/gh-check)
    python -m agent_gh store <agent> [--file key.pem]     (scripts/gh-store)
    python -m agent_gh bot-email <agent>
    python -m agent_gh credential reader get              (scripts/agent-gh reader credential get)
    python -m agent_gh gh talia pr merge|merge-check <N>  (Talia's merge on Ahmed's delegation, delegation.py)
    python -m agent_gh gh reader delegation-covers <time> (main-guard-check.sh: which delegation held then)

The token goes only into the environment of the one gh or git process that needs it (GH_TOKEN, or a
one-command credential helper for git), and an App's token is revoked as soon as that process ends,
whatever its exit code. It is never printed, and gh's own login is never touched. Trace variables that
could log it are cleared. AGENT_GH_DRY_RUN=1 prints what would run, and mints nothing.

`credential` is git's credential helper for the read-only App (reader): it answers `get` for
https://github.com only, with a read-only token for the one repository in the request's path, and
stores nothing. git doesn't tell a helper whether it fetches or pushes, so what keeps a push off is the
token itself: it carries read permissions only, and GitHub refuses a push with it.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from pathlib import Path

from . import delegation
from .check import run_check
from .identity import (
    IdentityError,
    acting_identity,
    agent_entry,
    bot_email,
    is_pending,
    load_config,
    read_permissions,
    revoke_installation_token,
    split_repo,
    token_for,
)
from .policy import Plan, plan_gh, plan_push, repo_from_remote, uses_push_app

# Answers git's `get` with the token from this process's environment, only for https://github.com, and
# stores nothing. git sends the request as key=value lines on stdin.
HELPER = (
    '!f() { test "$1" = get || return 0; p=; h=; while IFS= read -r l; do [ -z "$l" ] && break; '
    'case "$l" in protocol=*) p="${l#protocol=}";; host=*) h="${l#host=}";; esac; done; '
    '[ "$p" = https ] && [ "$h" = github.com ] && '
    'printf "username=x-access-token\\npassword=%s\\n" "$AGENT_GH_TOKEN"; return 0; }; f'
)
GH = "gh"
CLEARED = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS")
# Anything that could log the token or slip config in front of git's -c options.
CLEARED_PREFIXES = (
    "GIT_TRACE",
    "GIT_CURL_VERBOSE",
    "GH_DEBUG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_",
    "GIT_CONFIG_VALUE_",
)


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def _default_repo() -> str | None:
    if os.environ.get("GH_REPO"):
        return os.environ["GH_REPO"]
    url = _git("remote", "get-url", "origin")
    try:
        return repo_from_remote(url) if url else None
    except IdentityError:
        return None


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CLEARED and not k.startswith(CLEARED_PREFIXES)}
    env.update({"GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _dry_run(agent: str, tool: str, plan: Plan) -> int:
    perms = " ".join(f"{k}={v}" for k, v in sorted(plan.permissions.items()))
    print(f"agent-gh dry run: {agent} {tool} on {plan.repo} with {perms}: {' '.join(plan.args)}")
    return 0


def run_with_token(command: list[str], env: dict, token: str, revocable: bool, transport=None) -> int:
    """Runs the one command, then revokes an App token at once, even when the command failed."""
    try:
        return subprocess.run(command, env=env).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        if revocable:
            ok = revoke_installation_token(token, transport) if transport else revoke_installation_token(token)
            if not ok:
                print("agent-gh: could not revoke the token; it expires within the hour", file=sys.stderr)


def cmd_delegation(agent: str, args: list[str]) -> int:
    from .identity import api, urllib_transport

    atl = str(Path(__file__).resolve().parents[2] / "scripts" / "atl")
    deps = {
        "api": api,
        "transport": urllib_transport,
        "token_for": token_for,
        "revoke": revoke_installation_token,
        "read_permissions": read_permissions,
        "jira": delegation.jira_status_with_atl(atl, subprocess.run),
        "dry_run": bool(os.environ.get("AGENT_GH_DRY_RUN")),
        "out": print,
    }
    return delegation.cli(agent, args, load_config(), _default_repo(), deps)


def cmd_gh(agent: str, args: list[str]) -> int:
    if delegation.is_delegation_command(agent.lower(), args):
        return cmd_delegation(agent.lower(), args)
    config = load_config()
    name, entry = acting_identity(config, agent, uses_push_app(args))
    plan = plan_gh(name, entry, args, _default_repo(), config)
    if os.environ.get("AGENT_GH_DRY_RUN"):
        return _dry_run(name, "gh", plan)
    token, revocable = token_for(name, plan.repo, plan.permissions, config)
    env = _env()
    env.update({"GH_TOKEN": token, "GH_REPO": plan.repo})
    return run_with_token([GH, *plan.args], env, token, revocable)


def cmd_push(agent: str, args: list[str]) -> int:
    config = load_config()
    name, entry = acting_identity(config, agent, True)
    if entry.get("read_only"):
        raise IdentityError(f"refused: {entry['slug']} reads only; it never pushes")
    positionals = [a for a in args if not a.startswith("-")]
    remote = positionals[0] if positionals else "origin"
    if "://" in remote or remote.startswith("git@"):
        remote_url = remote
    else:
        # git pushes to every push URL of a remote, so there must be exactly one, and it is the one checked.
        urls = _git("remote", "get-url", "--push", "--all", remote).splitlines()
        if not urls:
            raise IdentityError(f"no git remote '{remote}' here")
        if len(urls) > 1:
            raise IdentityError(f"refused: the remote {remote} has {len(urls)} push URLs; agent-git pushes to one")
        remote_url = urls[0]
    branch = _git("symbolic-ref", "--short", "-q", "HEAD") or None
    plan = plan_push(args, branch, remote_url, config, entry)
    if os.environ.get("AGENT_GH_DRY_RUN"):
        return _dry_run(name, "push", plan)
    token, revocable = token_for(name, plan.repo, plan.permissions, config)
    env = _env()
    env["AGENT_GH_TOKEN"] = token
    git = [
        "git",
        "-c", "credential.helper=",  # drops every helper set before, including gh's and the Keychain's
        "-c", f"credential.helper={HELPER}",
        "-c", "url.https://github.com/.pushInsteadOf=git@github.com:",
        "-c", "url.https://github.com/.pushInsteadOf=ssh://git@github.com/",
        "-c", "push.recurseSubmodules=no",  # submodules would be pushed to their own remotes with this token
        "push", *plan.args,
    ]  # fmt: skip
    return run_with_token(git, env, token, revocable)


def credential_answer(agent: str, request: dict, config: dict, **mint) -> str:
    """git's `get` for the read-only App: username and password lines, or "" when it isn't github.com.

    The repository comes from the request's path (git sends it with credential.useHttpPath=true), so the
    token reaches that one repository only. It is not revoked: git uses it after the helper has returned,
    and it expires within the hour. It is minted with read permissions only, whatever the config says."""
    if request.get("protocol") != "https" or request.get("host") != "github.com":
        return ""
    name, entry = acting_identity(config, agent, False)
    if not entry.get("read_only"):
        raise IdentityError(
            f"refused: only a read-only App answers git's credential requests; {name} pushes with agent-git"
        )
    parts = [p for p in (request.get("path") or "").split("/") if p]
    if len(parts) < 2:
        raise IdentityError("no repository in git's request: set credential.https://github.com.useHttpPath true")
    owner, repo = split_repo(f"{parts[0]}/{parts[1][:-4] if parts[1].endswith('.git') else parts[1]}")
    token, _ = token_for(name, f"{owner}/{repo}", read_permissions(entry), config, **mint)
    return f"username=x-access-token\npassword={token}\n"


def cmd_credential(agent: str, args: list[str]) -> int:
    """git calls the helper with get, store or erase. Only get answers; nothing is ever stored."""
    if args[:1] != ["get"]:
        return 0
    request = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        request[key] = value
    answer = credential_answer(agent, request, load_config())
    sys.stdout.write(answer)
    return 0


def cmd_store(agent: str, args: list[str]) -> int:
    """Store an App key (from --file or the clipboard) or one of Yousef's tokens (from the clipboard)."""
    config = load_config()
    entry = agent_entry(config, agent)
    file = args[args.index("--file") + 1] if "--file" in args and args.index("--file") + 1 < len(args) else None
    if entry["kind"] == "app":
        service = entry["keychain"]
        raw = Path(file).read_bytes() if file else subprocess.run(["pbpaste"], capture_output=True).stdout
        raw = raw.strip()
        if not raw.startswith(b"-----BEGIN"):
            try:
                raw = base64.b64decode(raw, validate=True)
            except ValueError:
                raw = b""
        if not re.match(rb"-----BEGIN (RSA )?PRIVATE KEY-----", raw) or b"-----END" not in raw:
            print(f"Not a private key: nothing stored in {service}")
            return 1
        value = base64.b64encode(raw.strip() + b"\n").decode("ascii")
    else:
        owners = [a for a in args if a != "--file" and a != file]
        if len(owners) != 1 or owners[0] not in entry.get("tokens", {}):
            raise IdentityError(f"usage: gh-store {agent} <{'|'.join(entry.get('tokens', {}))}>")
        token_entry = entry["tokens"][owners[0]]
        service = token_entry["keychain"]
        value = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
        prefix = "ghp_" if token_entry.get("type") == "classic" else "github_pat_"
        if not value.startswith(prefix) or re.search(r"\s", value):
            print(f"Not a {token_entry.get('type')} token (starts with {prefix}): nothing stored in {service}")
            return 1
    # Through `security -i` on stdin, so the value never appears in the process list.
    result = subprocess.run(
        ["security", "-i"],
        input=f'add-generic-password -U -s {service} -a basita -w "{value}"\n',
        capture_output=True,
        text=True,
    )
    del value
    if result.returncode != 0 or (result.stderr or "").strip():
        print(f"FAIL the Keychain refused {service}")
        return 1
    print(f"stored {service}. Clear the clipboard (pbcopy </dev/null), then run scripts/gh-check {agent}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: agent_gh gh|push|store|bot-email|credential <agent> ... | check [agent...]"
    if not argv:
        print(usage, file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    try:
        if command == "check":
            lines, code = run_check(load_config(), rest or None)
            print("\n".join(lines))
            return code
        if not rest:
            print(usage, file=sys.stderr)
            return 2
        agent, args = rest[0].lower(), rest[1:]
        if command == "credential" or (command == "gh" and args[:1] == ["credential"]):
            return cmd_credential(agent, args[1:] if command == "gh" else args)
        if command == "gh":
            return cmd_gh(agent, args)
        if command == "push":
            return cmd_push(agent, args)
        if command == "store":
            return cmd_store(agent, args)
        if command == "bot-email":
            # A user agent commits as his push App's bot (Yousef as basita-yousef-docs[bot]); a pending App
            # has no bot yet, so its agent keeps <agent>@<agent domain>.
            agents = load_config().get("agents", {})
            entry = agents.get(agent) or {}
            if entry.get("kind") == "user" and entry.get("push_app"):
                entry = agents.get(entry["push_app"].lower()) or {}
            if entry.get("kind") != "app" or is_pending(entry) or not entry.get("bot_id"):
                return 1
            print(bot_email(entry))
            return 0
    except IdentityError as error:
        print(f"agent-gh: {error}", file=sys.stderr)
        return 1
    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
