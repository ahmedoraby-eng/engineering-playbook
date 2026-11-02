"""agent-gh, agent-git push, gh-check and gh-store, as one agent's GitHub identity.

    python -m agent_gh gh <agent> <gh arguments...>      (scripts/agent-gh)
    python -m agent_gh push <agent> <git push arguments...>  (scripts/agent-git <agent> push)
    python -m agent_gh check [agent...]                   (scripts/gh-check)
    python -m agent_gh store <agent> [--file key.pem]     (scripts/gh-store)
    python -m agent_gh bot-email <agent>

The token goes only into the environment of the one gh or git process that needs it (GH_TOKEN, or a
one-command credential helper for git). It is never printed, and gh's own login is never touched.
AGENT_GH_DRY_RUN=1 prints what would run, and mints nothing.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from pathlib import Path

from .check import run_check
from .identity import IdentityError, agent_entry, bot_email, load_config, token_for
from .policy import Plan, plan_gh, plan_push, repo_from_remote

# Answers git's `get` with the token from this process's environment, and stores nothing.
HELPER = '!f() { test "$1" = get && printf "username=x-access-token\\npassword=%s\\n" "$AGENT_GH_TOKEN"; }; f'
CLEARED = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS")


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
    env = {k: v for k, v in os.environ.items() if k not in CLEARED}
    env.update({"GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _dry_run(agent: str, tool: str, plan: Plan) -> int:
    perms = " ".join(f"{k}={v}" for k, v in sorted(plan.permissions.items()))
    print(f"agent-gh dry run: {agent} {tool} on {plan.repo} with {perms}: {' '.join(plan.args)}")
    return 0


def cmd_gh(agent: str, args: list[str]) -> int:
    config = load_config()
    entry = agent_entry(config, agent)
    plan = plan_gh(agent, entry, args, _default_repo(), config)
    if os.environ.get("AGENT_GH_DRY_RUN"):
        return _dry_run(agent, "gh", plan)
    token = token_for(agent, plan.repo, plan.permissions, config)
    env = _env()
    env.update({"GH_TOKEN": token, "GH_REPO": plan.repo})
    del token
    gh = os.environ.get("AGENT_GH_GH_BIN", "gh")
    os.execvpe(gh, [gh, *plan.args], env)
    return 0  # not reached


def cmd_push(agent: str, args: list[str]) -> int:
    config = load_config()
    entry = agent_entry(config, agent)
    if entry["kind"] != "app":
        raise IdentityError(f"refused: {entry.get('login', agent)} reviews and doesn't push")
    positionals = [a for a in args if not a.startswith("-")]
    remote = positionals[0] if positionals else "origin"
    remote_url = remote if "://" in remote or remote.startswith("git@") else _git("remote", "get-url", "--push", remote)
    if not remote_url:
        raise IdentityError(f"no git remote '{remote}' here")
    branch = _git("symbolic-ref", "--short", "-q", "HEAD") or None
    plan = plan_push(args, branch, remote_url, config)
    if os.environ.get("AGENT_GH_DRY_RUN"):
        return _dry_run(agent, "push", plan)
    token = token_for(agent, plan.repo, plan.permissions, config)
    env = _env()
    env["AGENT_GH_TOKEN"] = token
    del token
    git = [
        "git",
        "-c", "credential.helper=",  # drops every helper set before, including gh's and the Keychain's
        "-c", f"credential.helper={HELPER}",
        "-c", "url.https://github.com/.pushInsteadOf=git@github.com:",
        "-c", "url.https://github.com/.pushInsteadOf=ssh://git@github.com/",
        "push", *plan.args,
    ]  # fmt: skip
    os.execvpe("git", git, env)
    return 0  # not reached


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
    result = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", service, "-a", "basita", "-w", value], capture_output=True
    )
    del value
    if result.returncode != 0:
        print(f"FAIL the Keychain refused {service}")
        return 1
    print(f"stored {service}. Clear the clipboard (pbcopy </dev/null), then run scripts/gh-check {agent}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: agent_gh gh|push|store|bot-email <agent> ... | check [agent...]"
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
        if command == "gh":
            return cmd_gh(agent, args)
        if command == "push":
            return cmd_push(agent, args)
        if command == "store":
            return cmd_store(agent, args)
        if command == "bot-email":
            entry = load_config().get("agents", {}).get(agent)
            if not entry or entry["kind"] != "app":
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
