"""gh-app-new: create one agent's GitHub App from a manifest, and keep only its key (BASITA-158, D21).

    python -m agent_gh.app_new <agent> [--dry-run]      (scripts/gh-app-new)

The agent must be an App in config/github-identities.json that is not created yet (`app_id` null). The
script builds GitHub's App manifest from that entry: its name, description and `public` flag, its own
permissions (or the top-level `app_permissions`), no events, and the webhook off. GitHub only takes a
manifest as a form POST, so a tiny page on 127.0.0.1 posts it to the organisation's "new App" page in the
browser. Ahmed clicks Create there. GitHub sends the browser back to that local server with a one-time
code and the `state` the script made; any other state is refused. The code is traded once, without
authentication, at POST /app-manifests/{code}/conversions. The private key in GitHub's answer goes to
the Keychain the way gh-store stores one (`security -i` on stdin, base64 of the PEM on one line, account
`basita`). The key, the client secret and the webhook secret are then dropped: only the App id, client
id, slug and page are kept and printed, with the config lines to add and the install link.

Nothing secret is written to disk, printed or put in a process's arguments. The server listens on
127.0.0.1 only, takes one callback and stops, or gives up after 10 minutes. `--dry-run` prints the
manifest and the URL, and neither listens nor opens anything.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import re
import secrets
import socketserver
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, urlsplit

from .identity import ACCOUNT, API, IdentityError, agent_entry, load_config, urllib_transport

if TYPE_CHECKING:  # the CLI also runs on macOS's system Python 3.9
    from collections.abc import Callable

    from .identity import Transport

OWNER = "basitasolutions"  # every agent App is owned by the organisation (plan 10.2)
HOMEPAGE = f"https://github.com/{OWNER}"
NEW_APP = f"https://github.com/organizations/{OWNER}/settings/apps/new"
HOST = "127.0.0.1"
TIMEOUT = 600  # seconds to wait for Ahmed's click on Create
CALLBACK = "/callback"
CODE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# D4 and plan 10.2: no App gets these, whatever the config says.
FORBIDDEN = ("workflows", "administration", "secrets", "actions_variables", "environments")
# The only fields of GitHub's answer that are kept. Everything else, the key and secrets first, is dropped.
KEPT = ("id", "client_id", "slug", "name", "html_url", "permissions", "events")


def pending_app(config: dict, agent: str) -> dict:
    """The agent's entry, if it is an App that GitHub doesn't have yet."""
    entry = agent_entry(config, agent)
    if entry.get("kind") != "app":
        raise IdentityError(f"refused: {agent} is not an App in the config; gh-app-new creates Apps only")
    if entry.get("app_id"):
        raise IdentityError(f"refused: {entry.get('slug', agent)} already exists (app_id {entry['app_id']})")
    if not entry.get("slug"):
        raise IdentityError(f"refused: {agent} has no slug in the config")
    return entry


def _manifest_field(entry: dict, field: str, default):
    """A manifest field from the entry's `manifest` object, or from the entry itself."""
    manifest = entry.get("manifest") if isinstance(entry.get("manifest"), dict) else {}
    return manifest.get(field, entry.get(field, default))


def app_permissions(entry: dict, config: dict) -> dict:
    permissions = dict(entry.get("permissions") or config.get("app_permissions") or {})
    if not permissions:
        raise IdentityError(f"refused: {entry['slug']} has no permissions in the config")
    for name, access in permissions.items():
        if access not in ("read", "write"):
            raise IdentityError(f"refused: {entry['slug']} permission {name}={access} is not read or write")
        if name in FORBIDDEN or name.startswith("organization_") or (name == "actions" and access != "read"):
            raise IdentityError(f"refused: {entry['slug']} must not get {name}={access} (D4)")
    if permissions.get("metadata") != "read":
        raise IdentityError(f"refused: {entry['slug']} needs metadata=read, and nothing more on metadata")
    return permissions


def build_manifest(entry: dict, config: dict, redirect_url: str) -> dict:
    """GitHub's App manifest for the entry: no webhook, no events, no user authorisation."""
    public = _manifest_field(entry, "public", False)
    if not isinstance(public, bool):
        raise IdentityError(f"refused: {entry['slug']} has a 'public' that is not true or false")
    return {
        "name": _manifest_field(entry, "name", entry["slug"]),
        "url": HOMEPAGE,
        "description": _manifest_field(entry, "description", ""),
        "public": public,
        "hook_attributes": {"url": HOMEPAGE, "active": False},
        "redirect_url": redirect_url,
        "default_events": [],
        "default_permissions": app_permissions(entry, config),
        "request_oauth_on_install": False,
    }


def new_app_url(state: str) -> str:
    return f"{NEW_APP}?state={quote(state, safe='')}"


def start_page(post_url: str, manifest: dict) -> bytes:
    """A page that posts the manifest to GitHub as soon as it loads (a button if scripts are off)."""
    value = html.escape(json.dumps(manifest), quote=True)
    return (
        "<!doctype html><meta charset='utf-8'><title>gh-app-new</title>"
        f"<form id='f' method='post' action='{html.escape(post_url, quote=True)}'>"
        f"<input type='hidden' name='manifest' value='{value}'>"
        f"<p>Sending the manifest of {html.escape(manifest['name'])} to GitHub...</p>"
        "<button type='submit'>Continue to GitHub</button></form>"
        "<script>document.getElementById('f').submit()</script>"
    ).encode()


def _page(text: str) -> bytes:
    return f"<!doctype html><meta charset='utf-8'><title>gh-app-new</title><p>{html.escape(text)}</p>".encode()


def exchange_code(code: str, transport: Transport) -> dict:
    """Trades the one-time code for the new App's credentials. No message here names the code."""
    if not CODE.match(code or ""):
        raise IdentityError("refused: GitHub's code has an unexpected shape; nothing exchanged")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "basita-gh-app-new",
        "Content-Type": "application/json",
    }
    status, _, text = transport("POST", f"{API}/app-manifests/{code}/conversions", headers, b"{}")
    try:
        answer = json.loads(text) if text else {}
    except ValueError:
        answer = {}
    if not 200 <= status < 300 or not isinstance(answer, dict):
        message = answer.get("message") if isinstance(answer, dict) else ""
        raise IdentityError(f"GitHub said {status} to the code exchange: {message or 'no message'}")
    if not answer.get("pem") or not answer.get("id"):
        raise IdentityError("GitHub's answer has no App id or private key")
    return answer


def store_key(service: str, pem: str, run: Callable = subprocess.run) -> bool:
    """Stores the PEM as gh-store does: base64 on one line, through `security -i` on stdin."""
    raw = pem.encode().strip()
    if not re.match(rb"-----BEGIN (RSA )?PRIVATE KEY-----", raw) or b"-----END" not in raw:
        return False
    value = base64.b64encode(raw + b"\n").decode("ascii")
    result = run(
        ["security", "-i"],
        input=f'add-generic-password -U -s {service} -a {ACCOUNT} -w "{value}"\n',
        capture_output=True,
        text=True,
    )
    del value
    return result.returncode == 0 and not (result.stderr or "").strip()


def bot_id(slug: str, transport: Transport) -> int | None:
    """The App's bot user id, from the public users endpoint. None if GitHub doesn't answer."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "basita-gh-app-new"}
    try:
        status, _, text = transport("GET", f"{API}/users/{quote(slug + '[bot]', safe='')}", headers, None)
        answer = json.loads(text) if 200 <= status < 300 and text else {}
    except (IdentityError, ValueError):
        return None
    return answer.get("id") if isinstance(answer, dict) and isinstance(answer.get("id"), int) else None


class Flow:
    """One App creation: the start page, then one callback. Holds no secret after the callback."""

    def __init__(
        self,
        agent: str,
        entry: dict,
        manifest: dict,
        state: str,
        start_path: str,
        transport: Transport = urllib_transport,
        run: Callable = subprocess.run,
    ) -> None:
        self.agent, self.entry, self.manifest = agent, entry, manifest
        self.state, self.start_path = state, start_path
        self.transport, self.run = transport, run
        self.done = False
        self.result: dict | None = None
        self.error: str | None = None
        self.bot_id: int | None = None

    def handle(self, method: str, target: str) -> tuple[int, dict, bytes]:
        """(status, headers, body) for one request to the local server."""
        parts = urlsplit(target)
        if method != "GET":
            return 405, {}, _page("Not allowed.")
        if parts.path == self.start_path and not self.done:
            return 200, {}, start_page(new_app_url(self.state), self.manifest)
        if parts.path != CALLBACK or self.done:
            return 404, {}, _page("Not found.")
        self.done = True  # one callback, whatever it says
        query = parse_qs(parts.query)
        state = (query.get("state") or [""])[0]
        if not hmac.compare_digest(state.encode(), self.state.encode()):
            self.error = "refused: the state GitHub sent back is not this run's; nothing exchanged"
            return 400, {}, _page(self.error)
        try:
            self.result = self._create((query.get("code") or [""])[0])
        except IdentityError as error:
            self.error = str(error)
            return 500, {}, _page(f"{self.error}. See the terminal.")
        except Exception as error:  # only its type is shown: a message could quote GitHub's answer
            self.error = (
                f"unexpected {type(error).__name__} after GitHub's answer; nothing was printed. If GitHub created "
                f"the App, generate a private key in its settings and store it with scripts/gh-store {self.agent}"
            )
            return 500, {}, _page(f"{self.error}.")
        return 302, {"Location": f"{self.result['html_url']}/installations/new"}, b""

    def _create(self, code: str) -> dict:
        answer = exchange_code(code, self.transport)
        pem = answer.pop("pem")
        for field in ("client_secret", "webhook_secret"):
            answer.pop(field, None)
        kept = {k: answer[k] for k in KEPT if k in answer}
        del answer
        service = self.entry.get("keychain") or f"basita-gh-{self.agent}-app"
        stored = store_key(service, pem, self.run)
        del pem
        if not re.match(r"^https://github\.com/apps/[A-Za-z0-9-]+$", str(kept.get("html_url", ""))):
            kept["html_url"] = f"https://github.com/apps/{kept.get('slug') or self.entry['slug']}"  # the redirect
        kept["keychain"] = service
        if not stored:
            raise IdentityError(
                f"GitHub created {kept.get('slug')} (app id {kept['id']}), but the Keychain refused {service}. "
                f"Generate a private key on https://github.com/organizations/{OWNER}/settings/apps/"
                f"{kept.get('slug') or self.entry['slug']} and store it with scripts/gh-store {self.agent}"
            )
        self.bot_id = bot_id(kept.get("slug") or self.entry["slug"], self.transport)
        return kept


def report(flow: Flow) -> list[str]:
    """What to tell Ahmed and Malek after a successful run. Ids only."""
    result, entry, agent = flow.result or {}, flow.entry, flow.agent
    slug = result.get("slug") or entry["slug"]
    lines = [
        f"created {slug}: app id {result['id']}, client id {result.get('client_id')}, "
        f"bot id {flow.bot_id if flow.bot_id is not None else 'unknown (scripts/gh-check shows it)'}",
        f"  page {result['html_url']}",
        f"  private key stored in the Keychain as {result['keychain']} (account {ACCOUNT}); "
        "the key, client secret and webhook secret were not kept",
    ]
    if slug != entry["slug"]:
        lines.append(f"  attention GitHub named it {slug}, not {entry['slug']}: the config's slug must change too")
    if result.get("permissions") not in (None, flow.manifest["default_permissions"]):
        lines.append("  attention GitHub's permissions differ from the manifest: scripts/gh-check will say how")
    if result.get("events"):
        lines.append(f"  attention it subscribes to events {result['events']}: there should be none")
    fields = {"app_id": result["id"], "client_id": result.get("client_id"), "bot_id": flow.bot_id}
    if slug != entry["slug"]:
        fields = {"slug": slug, **fields}
    lines.append(f'Add to config/github-identities.json, "agents" > "{agent}" (replacing the nulls):')
    lines += [f"  {json.dumps(k)}: {json.dumps(v)}," for k, v in fields.items()]
    lines += [
        f"Install it: {result['html_url']}/installations/new (the browser is there now): choose the account,",
        f"  then 'Only select repositories'. scripts/gh-check {agent} then lists each installation with its id;",
        '  add each one as "installations": {"<owner>": {"id": <id>, "repos": [...]}}.',
    ]
    return lines


class _Server(HTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # skips HTTPServer's reverse lookup of the host name
        self.server_name, self.server_port = HOST, self.server_address[1]


def _handler(flow: Flow) -> type:
    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # a stalled connection can't hold the one-callback server forever
        server_version = "gh-app-new"
        sys_version = ""

        def _answer(self, method: str) -> None:
            status, headers, body = flow.handle(method, self.path)
            self.send_response(status)
            for name, value in {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}.items():
                self.send_header(name, value)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # http.server's name
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def log_message(self, *_args) -> None:  # the request line carries the one-time code: never log it
            pass

    return Handler


def serve(flow: Flow, server: HTTPServer, timeout: float) -> None:
    """Answers requests until the one callback is handled, or the time is up."""
    deadline = time.monotonic() + timeout
    server.timeout = 1
    while not flow.done and time.monotonic() < deadline:
        server.handle_request()


def main(
    argv: list[str] | None = None,
    config: dict | None = None,
    transport: Transport = urllib_transport,
    run: Callable = subprocess.run,
    opener: Callable | None = None,
    timeout: float = TIMEOUT,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    names = [a for a in argv if a != "--dry-run"]
    if len(names) != 1 or names[0].startswith("-"):
        print("usage: gh-app-new <agent> [--dry-run]", file=sys.stderr)
        return 2
    agent = names[0].lower()
    try:
        config = config if config is not None else load_config()
        entry = pending_app(config, agent)
        if dry_run:
            manifest = build_manifest(entry, config, f"http://{HOST}:<port>{CALLBACK}")
            print(f"gh-app-new dry run for {agent}: POST the manifest as the form field 'manifest' to")
            print(f"  {NEW_APP}?state=<random, made at each run>")
            print(json.dumps(manifest, indent=2))
            return 0
        server = _Server((HOST, 0), BaseHTTPRequestHandler)
    except IdentityError as error:
        print(f"gh-app-new: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"gh-app-new: cannot listen on {HOST}: {error}", file=sys.stderr)
        return 1
    try:
        port = server.server_address[1]
        manifest = build_manifest(entry, config, f"http://{HOST}:{port}{CALLBACK}")
        flow = Flow(agent, entry, manifest, secrets.token_urlsafe(32), f"/{secrets.token_urlsafe(16)}", transport, run)
        server.RequestHandlerClass = _handler(flow)
        local = f"http://{HOST}:{port}{flow.start_path}"
        visibility = "public (any account may install it)" if manifest["public"] else "private to " + OWNER
        print(f"gh-app-new: creating {manifest['name']} for {agent} on {OWNER}, {visibility}, webhook off")
        print(f"  permissions {', '.join(f'{k}={v}' for k, v in sorted(manifest['default_permissions'].items()))}")
        print(f"  opening {local} in the browser signed in to GitHub as an owner of {OWNER}; click Create there.")
        print(f"  Waiting up to {int(timeout)} seconds.")
        (opener or (lambda url: run(["open", url], capture_output=True)))(local)
        serve(flow, server, timeout)
    except IdentityError as error:
        print(f"gh-app-new: {error}", file=sys.stderr)
        return 1
    finally:
        server.server_close()
    if not flow.done:
        print(f"gh-app-new: no answer from GitHub in {int(timeout)} s; nothing created here. Run it again.")
        return 1
    if flow.error:
        print(f"gh-app-new: {flow.error}", file=sys.stderr)
        return 1
    print("\n".join(report(flow)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
