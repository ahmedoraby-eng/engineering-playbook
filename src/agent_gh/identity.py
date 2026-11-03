"""Who an agent is on GitHub, and a short-lived token for one command.

An App agent (Malek, Haya, Salma; Talia, Sherif, Adam, Nora, sync and reader once set up) has a
private key in the Keychain (`basita-gh-<agent>-app`, base64 of the PEM on one line, account `basita`).
In a cloud session, where there is no Keychain, an App other than sync and reader reads its own cloud
key from `BASITA_GH_<AGENT>_KEY` instead, in the same form (plan 10.8, BASITA-163).
For each command the key is read, signs a JWT through `openssl` from a pipe (nothing on disk), and the
JWT asks GitHub for an installation token limited to the one repository and the permissions that
command needs. That token lasts one hour and is kept
only in memory. A user agent (Yousef) has a personal access token per repository owner, and pushes
and opens pull requests with his `push_app`.

An App whose `app_id` is null in the config is pending: Ahmed has not created it yet (setup S2), and
every command with it is refused.

Tokens are only minted for the installations listed in config/github-identities.json, so an App
installed somewhere else is never used. No message here contains a token.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the CLI also runs on macOS's system Python 3.9
    from collections.abc import Callable

    # (method, url, headers, body) -> (status, response headers, response text). Swapped out in tests.
    Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, str]]

API = "https://api.github.com"
CONFIG = Path(__file__).resolve().parents[2] / "config" / "github-identities.json"
ACCOUNT = "basita"

# What each kind of command may do. metadata:read comes with every installation token.
READ = {"contents": "read", "pull_requests": "read", "metadata": "read"}
PR_WRITE = {"contents": "read", "pull_requests": "write", "metadata": "read"}
PUSH = {"contents": "write", "metadata": "read"}
LIST_ONLY = {"metadata": "read"}

REPO = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


class IdentityError(Exception):
    """A refused or failed step. The message never contains a token or a key."""


def load_config(path: Path | None = None) -> dict:
    path = Path(path or CONFIG)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise IdentityError(f"cannot read the identity config {path}: {error}") from None


def agent_entry(config: dict, agent: str) -> dict:
    entry = config.get("agents", {}).get(agent.lower())
    if not entry:
        known = ", ".join(sorted(config.get("agents", {})))
        raise IdentityError(f"{agent} has no GitHub identity (agents with one: {known}); use gh as before")
    return entry


def is_pending(entry: dict) -> bool:
    """An App listed in the config that Ahmed has not created yet (setup S2)."""
    return entry.get("kind") == "app" and entry.get("app_id") is None


def require_ready(entry: dict) -> None:
    if is_pending(entry):
        raise IdentityError(f"refused: {entry.get('slug')} is not set up yet (Ahmed's setup S2)")


def push_app_owner(config: dict, agent: str) -> str | None:
    """The user agent whose `push_app` this App is (yousef-docs is Yousef's), or None."""
    for name, entry in config.get("agents", {}).items():
        if entry.get("kind") == "user" and (entry.get("push_app") or "").lower() == agent.lower():
            return name
    return None


def acting_identity(config: dict, agent: str, writes_branch: bool) -> tuple[str, dict]:
    """The identity a command runs as, ready to use, or a refusal.

    A user agent (Yousef) reviews and comments with his own token; a push or a pull request he opens or
    edits (`writes_branch`) goes through his `push_app`, so his user never pushes. That App is reached
    only through him. A pending App is refused here, before anything is planned or minted."""
    entry = agent_entry(config, agent)
    owner = push_app_owner(config, agent)
    if owner:
        raise IdentityError(
            f"refused: {entry.get('slug', agent)} is {owner}'s; use agent-gh {owner} or agent-git {owner}"
        )
    if entry["kind"] == "user" and writes_branch:
        if not entry.get("push_app"):
            raise IdentityError(f"refused: {entry.get('login', agent)} reviews and doesn't push")
        name = entry["push_app"].lower()
        entry = agent_entry(config, name)
        require_ready(entry)
        return name, entry
    require_ready(entry)
    return agent.lower(), entry


def read_permissions(entry: dict) -> dict:
    """The token for a read. A read-only App (reader) gets its own read permissions, which are checked to
    be reads only, so a config edit can never turn its token into a writing one. Others get READ."""
    if not entry.get("read_only"):
        return READ
    permissions = dict(entry.get("permissions") or {})
    if not permissions or any(access != "read" for access in permissions.values()):
        raise IdentityError(f"refused: {entry.get('slug')} is read-only, but its config permissions are not all read")
    return permissions


def bot_login(entry: dict) -> str:
    return f"{entry['slug']}[bot]"


def bot_email(entry: dict) -> str:
    return f"{entry['bot_id']}+{bot_login(entry)}@users.noreply.github.com"


class NoKeychain(IdentityError):
    """This machine has no macOS Keychain (no `security` binary): a cloud session, or CI."""


def keychain_secret(service: str, run: Callable = subprocess.run) -> str:
    try:
        result = run(
            ["security", "find-generic-password", "-s", service, "-a", ACCOUNT, "-w"], capture_output=True, text=True
        )
    except FileNotFoundError:
        raise NoKeychain(f"no Keychain here (no `security`): {service} is kept on the Mac only") from None
    value = (result.stdout or "").strip()
    if result.returncode != 0 or not value:
        raise IdentityError(f"no Keychain item {service} (account '{ACCOUNT}'); see standards/06-agent-identity.md")
    return value


def _pem_from_base64(value: str, where: str) -> bytes:
    """A PEM private key from one line of base64. The messages name `where`, never the value."""
    if value.lstrip().startswith("-----BEGIN"):
        raise IdentityError(f"{where} holds a PEM, not one base64 line; make the line with scripts/gh-cloud-key")
    try:
        pem = base64.b64decode(value, validate=True)
    except ValueError:
        raise IdentityError(f"{where} does not hold base64 of a private key") from None
    if not re.match(rb"-----BEGIN (RSA )?PRIVATE KEY-----", pem):
        raise IdentityError(f"{where} does not hold a private key")
    return pem.strip() + b"\n"


def private_key(service: str, run: Callable = subprocess.run) -> bytes:
    """The App's PEM key, decoded from the one-line base64 the Keychain holds."""
    return _pem_from_base64(keychain_secret(service, run), service)


def cloud_key_variable(agent: str) -> str:
    """The environment variable of an App's cloud key: BASITA_GH_MALEK_KEY, BASITA_GH_YOUSEF_DOCS_KEY."""
    return "BASITA_GH_" + re.sub(r"[^A-Z0-9]", "_", agent.upper()) + "_KEY"


def never_in_cloud(agent: str, entry: dict) -> bool:
    """sync and reader, and any App that updates main or reads only, have no key in the cloud (plan 10.8)."""
    return agent.lower() in ("sync", "reader") or bool(entry.get("updates_main") or entry.get("read_only"))


def env_private_key(agent: str, entry: dict, environ: dict | None = None) -> bytes:
    """The App's cloud key from BASITA_GH_<AGENT>_KEY: its own second key, one base64 line of the PEM
    (plan 10.8, D19a). Refused for sync and reader even when the variable is set. A user agent (Yousef)
    has no cloud key: his tokens stay in the Mac's Keychain."""
    environ = os.environ if environ is None else environ
    name = cloud_key_variable(agent)
    if entry.get("kind") != "app":
        raise IdentityError(f"refused: {entry.get('login', agent)}'s tokens are kept in the Mac's Keychain only")
    if never_in_cloud(agent, entry):
        raise IdentityError(f"refused: {entry.get('slug', agent)} never has a key in the cloud (plan 10.8)")
    value = (environ.get(name) or "").strip()
    if not value:
        slug = entry.get("slug", agent)
        raise IdentityError(f"no Keychain here and no {name}: {slug} has no cloud key in this environment")
    return _pem_from_base64(value, name)


def app_key_with_source(
    agent: str, entry: dict, run: Callable = subprocess.run, environ: dict | None = None
) -> tuple[bytes, str]:
    """The App's key and where it came from. The Keychain always wins: the variable is read only when
    this machine has no Keychain at all, never because a Keychain item is missing."""
    try:
        return private_key(entry["keychain"], run), entry["keychain"]
    except NoKeychain:
        return env_private_key(agent, entry, environ), cloud_key_variable(agent)


def app_key(agent: str, entry: dict, run: Callable = subprocess.run, environ: dict | None = None) -> bytes:
    return app_key_with_source(agent, entry, run, environ)[0]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_rs256(pem: bytes, data: bytes, openssl: str = "openssl") -> bytes:
    """RS256 signature by openssl, which reads the key from a pipe, so the key is never written to disk."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, pem)  # a PEM key is far below the pipe buffer
        os.close(write_end)
        write_end = -1
        result = subprocess.run(
            [openssl, "dgst", "-sha256", "-sign", f"/dev/fd/{read_end}"],
            input=data,
            capture_output=True,
            pass_fds=(read_end,),
        )
    finally:
        os.close(read_end)
        if write_end != -1:
            os.close(write_end)
    if result.returncode != 0 or not result.stdout:
        raise IdentityError("openssl could not sign with the App key (is the key complete?)")
    return result.stdout


def app_jwt(entry: dict, pem: bytes, now: float | None = None, openssl: str = "openssl") -> str:
    """A 9-minute JWT for the App. iat is 60 s early for clock drift, as GitHub advises."""
    now = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"iat": now - 60, "exp": now + 540, "iss": entry.get("client_id") or str(entry["app_id"])}
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_b64url(sign_rs256(pem, signing_input, openssl))}"


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read().decode("utf-8", "replace")
    except urllib.error.URLError as error:
        raise IdentityError(f"GitHub is not reachable: {error.reason}") from None


def api(transport: Transport, method: str, path: str, auth: str, body: dict | None = None) -> tuple[dict, dict]:
    """One GitHub REST call. Returns (headers, JSON). Errors name the status and GitHub's message only."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "basita-agent-gh",
    }
    if auth:
        headers["Authorization"] = auth
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    status, response_headers, text = transport(method, f"{API}{path}", headers, data)
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    if not 200 <= status < 300:
        message = payload.get("message") if isinstance(payload, dict) else ""
        raise IdentityError(f"GitHub said {status} to {method} {path.split('?')[0]}: {message or 'no message'}")
    return response_headers, payload


def split_repo(repo: str) -> tuple[str, str]:
    if not REPO.match(repo or ""):
        raise IdentityError(f"'{repo}' is not an owner/name repository")
    owner, name = repo.split("/")
    return owner, name


def installation_for(entry: dict, owner: str) -> int:
    """The listed installation for a repository owner. Any other owner is refused."""
    for listed_owner, installation in entry.get("installations", {}).items():
        if listed_owner.lower() == owner.lower():
            return int(installation["id"])
    listed = ", ".join(entry.get("installations", {})) or "none"
    raise IdentityError(f"{entry['slug']} has no listed installation for {owner} (listed: {listed})")


def mint_installation_token(
    entry: dict,
    installation_id: int,
    permissions: dict,
    repositories: list[str] | None,
    transport: Transport,
    jwt: str,
) -> dict:
    """POST access_tokens. Returns GitHub's answer: token, expires_at, permissions, repositories."""
    body: dict = {"permissions": permissions}
    if repositories is not None:
        body["repositories"] = repositories
    _, answer = api(transport, "POST", f"/app/installations/{installation_id}/access_tokens", f"Bearer {jwt}", body)
    if not answer.get("token"):
        raise IdentityError(f"GitHub gave {entry['slug']} no token for installation {installation_id}")
    return answer


def token_for(
    agent: str,
    repo: str,
    permissions: dict,
    config: dict,
    transport: Transport = urllib_transport,
    run: Callable = subprocess.run,
    openssl: str = "openssl",
) -> tuple[str, bool]:
    """A token for one command on one repository, as the agent, and whether it is an installation token
    (which the caller revokes when the command ends). Yousef's personal token is never revoked."""
    entry = agent_entry(config, agent)
    require_ready(entry)
    owner, name = split_repo(repo)
    if entry["kind"] == "user":
        for listed_owner, token in entry.get("tokens", {}).items():
            if listed_owner.lower() == owner.lower():
                try:
                    return keychain_secret(token["keychain"], run), False
                except NoKeychain:
                    raise IdentityError(
                        f"{entry['login']}'s tokens are kept in the Mac's Keychain only; there is no cloud copy"
                    ) from None
        raise IdentityError(f"{entry['login']} has no token for {owner} (listed: {', '.join(entry.get('tokens', {}))})")
    if entry.get("read_only") and any(access != "read" for access in permissions.values()):
        raise IdentityError(f"refused: {entry['slug']} reads only; it gets no token that writes")
    installation_id = installation_for(entry, owner)
    jwt = app_jwt(entry, app_key(agent, entry, run), openssl=openssl)
    return mint_installation_token(entry, installation_id, permissions, [name], transport, jwt)["token"], True


def revoke_installation_token(token: str, transport: Transport = urllib_transport) -> bool:
    """Ends an installation token before its hour is up. It authenticates with the token itself."""
    try:
        api(transport, "DELETE", "/installation/token", f"token {token}")
        return True
    except IdentityError:
        return False
