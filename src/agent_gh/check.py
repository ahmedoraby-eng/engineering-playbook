"""gh-check: what each agent's GitHub identity is and reaches, without printing a token. Changes nothing.

Lines start with `ok`, `FAIL` or `attention`. FAIL means the identity doesn't work as the config says;
attention is something for Ahmed to look at (an App installed somewhere not listed, a token that
expires within 14 days). The exit code is 1 when any line is FAIL. An App Ahmed has not created yet
(setup S2) is one plain `pending <agent>: not created yet` line, neither FAIL nor attention. Each App
is checked against its own `permissions`, or the top-level `app_permissions` when it has none.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from typing import TYPE_CHECKING

from .identity import (
    LIST_ONLY,
    IdentityError,
    api,
    app_jwt,
    app_key_with_source,
    bot_login,
    is_pending,
    keychain_secret,
    mint_installation_token,
    revoke_installation_token,
    urllib_transport,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .identity import Transport

EXPIRY_WARNING_DAYS = 14


def _perms(permissions: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted((permissions or {}).items())) or "none"


def _days_left(expiry: str, today: dt.date) -> int | None:
    """Days until a `github-authentication-token-expiration` date ("2027-10-31 00:00:00 UTC")."""
    try:
        return (dt.date.fromisoformat(expiry.strip()[:10]) - today).days
    except ValueError:
        return None


def expected_permissions(entry: dict, config: dict) -> dict:
    return entry.get("permissions") or config.get("app_permissions", {})


def check_app(agent: str, entry: dict, config: dict, transport: Transport, run: Callable, today: dt.date) -> list[str]:
    lines = [f"{agent}: App {entry['slug']} (id {entry['app_id']})"]
    expected = expected_permissions(entry, config)
    if entry.get("read_only") and any(access != "read" for access in expected.values()):
        return [
            *lines,
            f"  FAIL the config gives the read-only {entry['slug']} {_perms(expected)}; it must be reads only",
        ]
    if not entry.get("bot_id"):
        return [*lines, f"  FAIL the config has no bot_id for {entry['slug']}"]
    try:
        pem, source = app_key_with_source(agent, entry, run)
        lines.append(f"  ok key: {source} holds a private key")
        jwt = app_jwt(entry, pem)
        del pem
        auth = f"Bearer {jwt}"
        _, app = api(transport, "GET", "/app", auth)
    except IdentityError as error:
        return [*lines, f"  FAIL {error}"]
    granted = app.get("permissions") or {}
    if app.get("slug") != entry["slug"] or int(app.get("id", 0)) != int(entry["app_id"]):
        lines.append(f"  FAIL the key belongs to {app.get('slug')} (id {app.get('id')}), not {entry['slug']}")
    elif granted != expected:
        lines.append(f"  FAIL App permissions are {_perms(granted)}; the config expects {_perms(expected)}")
    else:
        lines.append(f"  ok App permissions: {_perms(granted)}; events: {', '.join(app.get('events') or []) or 'none'}")
    # §4.1a: Malek's App is public, so no event data may go anywhere. GitHub answers 404 when there is no webhook.
    try:
        _, hook = api(transport, "GET", "/app/hook/config", auth)
        if hook.get("url"):
            lines.append("  FAIL a webhook is configured; it must be off")
        else:
            lines.append("  ok webhook: none")
    except IdentityError as error:
        lines.append("  ok webhook: none" if " 404 " in str(error) else f"  FAIL webhook: {error}")
    installations: list = []
    page = 1
    while True:
        try:
            _, batch = api(transport, "GET", f"/app/installations?per_page=100&page={page}", auth)
        except IdentityError as error:
            return [*lines, f"  FAIL installations: {error}"]
        installations.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    listed = {int(i["id"]): owner for owner, i in entry.get("installations", {}).items()}
    seen = set()
    bot_check_token = None  # a public lookup; an installation token, once there is one, avoids the anonymous limit
    for installation in installations:
        iid = int(installation["id"])
        account = (installation.get("account") or {}).get("login", "?")
        seen.add(iid)
        if iid not in listed:
            lines.append(
                f"  attention installation {iid} on {account} is not in the config: agent-gh never uses it. "
                "Ahmed decides (uninstall it in GitHub settings, or list it)"
            )
            continue
        if listed[iid].lower() != account.lower():
            lines.append(f"  FAIL installation {iid} is on {account}; the config says {listed[iid]}")
            continue
        if installation.get("suspended_at"):
            lines.append(f"  FAIL installation {iid} on {account} is suspended")
            continue
        try:
            token = mint_installation_token(entry, iid, LIST_ONLY, None, transport, jwt)
        except IdentityError as error:
            lines.append(f"  FAIL installation {iid} on {account}: {error}")
            continue
        try:
            _, reached = api(transport, "GET", "/installation/repositories?per_page=100", f"token {token['token']}")
            if bot_check_token is None:
                bot_check_token = token["token"]  # kept for the bot lookup below, then revoked
            else:
                revoke_installation_token(token["token"], transport)
        except IdentityError as error:
            revoke_installation_token(token["token"], transport)
            lines.append(f"  FAIL installation {iid} on {account}: {error}")
            continue
        repos = sorted(r["name"] for r in reached.get("repositories", []))
        want = sorted(entry["installations"][listed[iid]].get("repos", []))
        state = "ok" if repos == want else "FAIL"
        lines.append(
            f"  {state} installation {iid} on {account} ({installation.get('repository_selection')}): "
            f"repos {', '.join(repos) or 'none'}"
            + ("" if state == "ok" else f"; the config says {', '.join(want) or 'none'}")
            + f"; permissions {_perms(installation.get('permissions'))}; its check token was revoked"
        )
    for iid, owner in listed.items():
        if iid not in seen:
            lines.append(f"  FAIL installation {iid} ({owner}) from the config is not installed")
    # A JWT can't read /users, so the bot is looked up after the installations.
    bot_auth = f"token {bot_check_token}" if bot_check_token else ""
    try:
        _, bot = api(transport, "GET", f"/users/{bot_login(entry).replace('[', '%5B').replace(']', '%5D')}", bot_auth)
        if int(bot.get("id", 0)) == int(entry["bot_id"]):
            lines.insert(3, f"  ok bot: {bot_login(entry)} id {entry['bot_id']}")
        else:
            lines.insert(3, f"  FAIL bot {bot_login(entry)} has id {bot.get('id')}; the config says {entry['bot_id']}")
    except IdentityError as error:
        lines.insert(3, f"  FAIL bot {bot_login(entry)}: {error}")
    if bot_check_token:
        revoke_installation_token(bot_check_token, transport)
    return lines


def check_user(agent: str, entry: dict, transport: Transport, run: Callable, today: dt.date) -> list[str]:
    lines = [f"{agent}: user {entry['login']}"]
    for owner, token_entry in entry.get("tokens", {}).items():
        service = token_entry["keychain"]
        try:
            auth = f"token {keychain_secret(service, run)}"
            headers, user = api(transport, "GET", "/user", auth)
        except IdentityError as error:
            lines.append(f"  FAIL {service}: {error}")
            continue
        login = user.get("login")
        if login != entry["login"]:
            lines.append(f"  FAIL {service} logs in as {login}, not {entry['login']}")
            continue
        expiry = headers.get("github-authentication-token-expiration", "")
        days = _days_left(expiry, today) if expiry else None
        when = f"expires {expiry[:10]} ({days} days)" if days is not None else "no expiry reported"
        detail = ""
        state = "ok"
        if token_entry.get("type") == "classic":
            scopes = sorted(s.strip() for s in headers.get("x-oauth-scopes", "").split(",") if s.strip())
            want = sorted(token_entry.get("scopes", []))
            detail = f"; scopes {', '.join(scopes) or 'none'}"
            if scopes != want:
                state = "FAIL"
                detail += f" (the config says {', '.join(want)})"
        # The repos he reaches as a member or collaborator of this owner: push anywhere is a FAIL (on a
        # personal account a collaborator always has push, so this also catches being added to the playbook).
        try:
            _, repos = api(transport, "GET", "/user/repos?per_page=100", auth)
            mine = sorted(
                f"{r['full_name']} ({'push' if (r.get('permissions') or {}).get('push') else 'read'})"
                for r in repos
                if r.get("owner", {}).get("login", "").lower() == owner.lower()
            )
            detail += f"; repos {', '.join(mine) or 'none'}"
            if any(m.endswith("(push)") for m in mine):
                state = "FAIL"
                detail += " (Yousef must not push)"
        except IdentityError as error:
            state, detail = "FAIL", f"{detail}; repos: {error}"
        lines.append(f"  {state} {service} ({owner}, {token_entry.get('type')}): login {login}; {when}{detail}")
        if days is not None and days <= EXPIRY_WARNING_DAYS:
            lines.append(f"  attention {service} expires in {days} days: Ahmed makes a new one and stores it")
    return lines


def run_check(
    config: dict,
    agents: list[str] | None = None,
    transport: Transport = urllib_transport,
    run: Callable = subprocess.run,
    today: dt.date | None = None,
) -> tuple[list[str], int]:
    today = today or dt.date.today()
    names = agents or list(config.get("agents", {}))
    lines: list[str] = []
    for name in names:
        entry = config.get("agents", {}).get(name.lower())
        if not entry:
            lines.append(f"{name}: FAIL no GitHub identity in the config")
        elif is_pending(entry):
            lines.append(f"pending {name.lower()}: not created yet")
        elif entry["kind"] == "app":
            lines.extend(check_app(name.lower(), entry, config, transport, run, today))
        else:
            lines.extend(check_user(name.lower(), entry, transport, run, today))
    failed = any("FAIL" in line.split() for line in lines)
    return lines, 1 if failed else 0
