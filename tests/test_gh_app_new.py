"""gh-app-new (BASITA-158): the manifest, the one callback, the code exchange and the Keychain store.

Nothing here reaches GitHub or the real Keychain: GitHub is a fake transport, `security` a fake runner,
and the private key a throwaway made by openssl for the test.
"""

import base64
import html
import http.client
import json
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from agent_gh import app_new
from agent_gh.identity import IdentityError

ROOT = Path(__file__).resolve().parents[1]
CLIENT_SECRET = "fake-client-secret-value"  # stand-ins, not real secrets
WEBHOOK_SECRET = "fake-webhook-secret-value"
CODE = "v1_onetimecode123"

CONFIG = {
    "app_permissions": {"contents": "write", "metadata": "read", "pull_requests": "write"},
    "agents": {
        "malek": {
            "kind": "app",
            "slug": "basita-malek",
            "app_id": 5586450,
            "client_id": "Iv23lixrAyMveN8xvGIK",
            "bot_id": 351705446,
            "keychain": "basita-gh-malek-app",
            "branch_prefix": "malek/",
            "manifest": {"name": "basita-malek", "public": True, "description": "Malek, Basita's engineer"},
            "installations": {"basitasolutions": {"id": 177981619, "repos": ["basita-ops"]}},
        },
        "talia": {
            "kind": "app",
            "slug": "basita-talia",
            "app_id": None,
            "client_id": None,
            "bot_id": None,
            "keychain": "basita-gh-talia-app",
            "branch_prefix": "talia/",
            "permissions": {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"},
            "manifest": {"name": "basita-talia", "public": False, "description": "Talia, Basita's coordinator"},
            "installations": {},
        },
        "reader": {
            "kind": "app",
            "slug": "basita-reader",
            "app_id": None,
            "client_id": None,
            "bot_id": None,
            "permissions": {
                "metadata": "read",
                "contents": "read",
                "pull_requests": "read",
                "issues": "read",
                "checks": "read",
                "statuses": "read",
                "actions": "read",
            },
            "manifest": {"name": "basita-reader", "public": True, "description": "Reads for Basita's Mac"},
            "installations": {},
        },
        # The manifest fields on the entry itself, and no permissions of its own: app_permissions applies.
        "sherif": {
            "kind": "app",
            "slug": "basita-sherif",
            "app_id": None,
            "public": False,
            "description": "Sherif, Basita's project manager",
            "installations": {},
        },
        "yousef": {"kind": "user", "login": "basita-yousef", "user_id": 351697344, "push_app": "yousef-docs"},
    },
}


@pytest.fixture(scope="module")
def pem(tmp_path_factory):
    folder = tmp_path_factory.mktemp("key")
    subprocess.run(["openssl", "genrsa", "-out", str(folder / "k.pem"), "2048"], check=True, capture_output=True)
    return (folder / "k.pem").read_text()


class FakeGitHub:
    """The conversion and users endpoints. Records each call."""

    def __init__(self, pem, status=201, slug="basita-talia", permissions=None, events=None):
        self.calls = []
        self.status = status
        self.answer = {
            "id": 7000001,
            "slug": slug,
            "name": slug,
            "client_id": "Iv23liFakeClient",
            "html_url": f"https://github.com/apps/{slug}",
            "owner": {"login": "basitasolutions"},
            "permissions": permissions
            or {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"},
            "events": events or [],
            "pem": pem,
            "client_secret": CLIENT_SECRET,
            "webhook_secret": WEBHOOK_SECRET,
        }

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if "/app-manifests/" in url:
            if self.status >= 300:
                return self.status, {}, json.dumps({"message": "Not Found"})
            return self.status, {}, json.dumps(self.answer)
        if "/users/" in url:
            return 200, {}, json.dumps({"login": f"{self.answer['slug']}[bot]", "id": 352000001})
        return 404, {}, "{}"


class FakeSecurity:
    """Stands in for `security -i`: keeps what came on stdin, and every command's argv."""

    def __init__(self, returncode=0, stderr=""):
        self.calls = []
        self.returncode, self.stderr = returncode, stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs.get("input")))
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr=self.stderr)

    def stored(self):
        """(service, account, decoded PEM) of each add-generic-password on stdin."""
        out = []
        for cmd, stdin in self.calls:
            if cmd[0] == "security":
                words = stdin.split()
                out.append((words[words.index("-s") + 1], words[words.index("-a") + 1], words[-1].strip('"')))
        return out


def flow_for(agent="talia", transport=None, run=None, pem=""):
    entry = app_new.pending_app(CONFIG, agent)
    manifest = app_new.build_manifest(entry, CONFIG, "http://127.0.0.1:5555/callback")
    return app_new.Flow(
        agent, entry, manifest, "the-state", "/start-token", transport or FakeGitHub(pem), run or FakeSecurity()
    )


def assert_nothing_secret(text, pem):
    body = "".join(pem.strip().splitlines()[1:-1])
    assert "PRIVATE KEY" not in text and body[:40] not in text
    assert base64.b64encode(pem.encode())[:40].decode() not in text
    assert CLIENT_SECRET not in text and WEBHOOK_SECRET not in text and CODE not in text


# --- the manifest ------------------------------------------------------------------------------


def test_manifest_has_the_webhook_off_no_events_and_the_entrys_own_permissions():
    entry = app_new.pending_app(CONFIG, "talia")
    manifest = app_new.build_manifest(entry, CONFIG, "http://127.0.0.1:5555/callback")
    assert manifest["name"] == "basita-talia"
    assert manifest["public"] is False
    assert manifest["description"] == "Talia, Basita's coordinator"
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["default_events"] == []
    assert manifest["default_permissions"] == CONFIG["agents"]["talia"]["permissions"]
    assert manifest["redirect_url"] == "http://127.0.0.1:5555/callback"
    assert manifest["request_oauth_on_install"] is False
    assert manifest["url"].startswith("https://")
    assert not {"callback_urls", "setup_url"} & set(manifest)


def test_public_comes_from_the_config_and_app_permissions_apply_without_the_entrys_own():
    reader = app_new.build_manifest(app_new.pending_app(CONFIG, "reader"), CONFIG, "x")
    assert reader["public"] is True
    assert set(reader["default_permissions"].values()) == {"read"}
    sherif = app_new.build_manifest(app_new.pending_app(CONFIG, "sherif"), CONFIG, "x")
    assert sherif["name"] == "basita-sherif" and sherif["public"] is False
    assert sherif["description"] == "Sherif, Basita's project manager"
    assert sherif["default_permissions"] == CONFIG["app_permissions"]


def test_the_start_page_posts_the_manifest_to_the_organisations_new_app_page():
    flow = flow_for()
    status, _, body = flow.handle("GET", "/start-token")
    page = body.decode()
    assert status == 200
    action = re.search(r"action='([^']+)'", page).group(1)
    assert html.unescape(action) == "https://github.com/organizations/basitasolutions/settings/apps/new?state=the-state"
    assert "method='post'" in page
    value = re.search(r"name='manifest' value='([^']+)'", page).group(1)
    assert json.loads(html.unescape(value)) == flow.manifest
    assert flow.done is False


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "agent, message",
    [
        ("malek", "already exists (app_id 5586450)"),
        ("yousef", "not an App in the config"),
        ("basel", "has no GitHub identity"),
    ],
)
def test_refuses_an_agent_that_is_not_a_pending_app(agent, message, capsys):
    with pytest.raises(IdentityError, match=re.escape(message)):
        app_new.pending_app(CONFIG, agent)
    assert app_new.main([agent, "--dry-run"], config=CONFIG) == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "permissions",
    [
        {"metadata": "read", "contents": "write", "workflows": "write"},
        {"metadata": "read", "administration": "read"},
        {"metadata": "read", "secrets": "read"},
        {"metadata": "read", "actions": "write"},
        {"metadata": "read", "organization_administration": "read"},
        {"metadata": "read", "contents": "admin"},
        {"contents": "write"},
    ],
)
def test_refuses_permissions_that_d4_forbids_or_that_are_malformed(permissions):
    config = json.loads(json.dumps(CONFIG))
    config["agents"]["talia"]["permissions"] = permissions
    with pytest.raises(IdentityError, match="refused"):
        app_new.build_manifest(app_new.pending_app(config, "talia"), config, "x")


def test_refuses_a_public_flag_that_is_not_a_boolean():
    config = json.loads(json.dumps(CONFIG))
    config["agents"]["talia"]["manifest"]["public"] = "yes"
    with pytest.raises(IdentityError, match="not true or false"):
        app_new.build_manifest(app_new.pending_app(config, "talia"), config, "x")


@pytest.mark.parametrize("argv", [[], ["talia", "reader"], ["--dry-run"]])
def test_usage(argv, capsys):
    assert app_new.main(argv, config=CONFIG) == 2
    assert "usage: gh-app-new" in capsys.readouterr().err


# --- dry run -----------------------------------------------------------------------------------


def test_dry_run_prints_the_manifest_and_url_and_neither_listens_nor_opens(capsys, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("a dry run must not run, open or listen")

    monkeypatch.setattr(app_new, "_Server", forbidden)
    assert app_new.main(["Talia", "--dry-run"], config=CONFIG, run=forbidden, opener=forbidden) == 0
    out = capsys.readouterr().out
    assert "https://github.com/organizations/basitasolutions/settings/apps/new?state=" in out
    manifest = json.loads(out[out.index("{") :])
    assert manifest["name"] == "basita-talia"
    assert manifest["hook_attributes"] == {"url": app_new.HOMEPAGE, "active": False}
    assert manifest["redirect_url"] == "http://127.0.0.1:<port>/callback"


def test_the_script_refuses_an_app_that_already_exists():
    result = subprocess.run(
        [str(ROOT / "scripts" / "gh-app-new"), "malek", "--dry-run"], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "basita-malek already exists" in result.stderr


# --- the callback ------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["code=abc&state=another-state", "code=abc", "code=abc&state="])
def test_a_callback_with_another_state_is_refused_and_nothing_is_exchanged(query, pem):
    github, security = FakeGitHub(pem), FakeSecurity()
    flow = flow_for(transport=github, run=security)
    status, _, body = flow.handle("GET", f"/callback?{query}")
    assert status == 400 and b"not this run" in body
    assert flow.done and "state" in flow.error and flow.result is None
    assert github.calls == [] and security.calls == []
    # One callback only: the right state afterwards is not taken either.
    assert flow.handle("GET", "/callback?code=abc&state=the-state")[0] == 404
    assert flow.handle("GET", "/start-token")[0] == 404
    assert github.calls == []


def test_other_paths_and_methods_are_not_a_callback(pem):
    flow = flow_for(pem=pem)
    assert flow.handle("GET", "/favicon.ico")[0] == 404
    assert flow.handle("POST", "/callback?code=abc&state=the-state")[0] == 405
    assert flow.done is False


def test_a_code_of_an_unexpected_shape_is_not_sent_to_github(pem):
    github = FakeGitHub(pem)
    flow = flow_for(transport=github)
    status, _, _ = flow.handle("GET", "/callback?code=..%2Fapp&state=the-state")
    assert status == 500 and "unexpected shape" in flow.error and github.calls == []


def test_the_code_is_exchanged_and_the_key_goes_only_to_security_on_stdin(pem, capsys):
    github, security = FakeGitHub(pem), FakeSecurity()
    flow = flow_for(transport=github, run=security)
    status, headers, body = flow.handle("GET", f"/callback?code={CODE}&state=the-state")
    assert status == 302
    assert headers["Location"] == "https://github.com/apps/basita-talia/installations/new"
    method, url, sent_headers, sent_body = github.calls[0]
    assert (method, url) == ("POST", f"https://api.github.com/app-manifests/{CODE}/conversions")
    assert "Authorization" not in sent_headers  # the exchange takes no authentication
    assert sent_body == b"{}"
    # The key: only on security's stdin, base64 of the PEM on one line, in the agent's item.
    assert [cmd for cmd, _ in security.calls] == [["security", "-i"]]
    [(service, account, value)] = security.stored()
    assert (service, account) == ("basita-gh-talia-app", "basita")
    assert base64.b64decode(value).decode() == pem.strip() + "\n"
    stdin = security.calls[0][1]
    assert stdin.count("\n") == 1 and stdin.startswith("add-generic-password -U -s basita-gh-talia-app -a basita -w")
    # Only the non-secret fields are kept.
    assert set(flow.result) <= set(app_new.KEPT) | {"keychain"}
    assert_nothing_secret(json.dumps(flow.result) + body.decode() + json.dumps(headers), pem)
    assert flow.bot_id == 352000001
    lines = "\n".join(app_new.report(flow))
    assert '"app_id": 7000001,' in lines and '"client_id": "Iv23liFakeClient",' in lines
    assert '"bot_id": 352000001,' in lines
    assert "https://github.com/apps/basita-talia/installations/new" in lines
    assert "  attention" not in lines
    assert_nothing_secret(lines + capsys.readouterr().out, pem)


def test_a_renamed_app_or_other_permissions_are_attention_lines(pem):
    github = FakeGitHub(pem, slug="basita-talia-2", permissions={"metadata": "read"}, events=["push"])
    flow = flow_for(transport=github)
    assert flow.handle("GET", f"/callback?code={CODE}&state=the-state")[0] == 302
    lines = "\n".join(app_new.report(flow))
    assert "GitHub named it basita-talia-2" in lines and '"slug": "basita-talia-2",' in lines
    assert "permissions differ" in lines and "events ['push']" in lines


def test_a_github_error_on_the_exchange_names_neither_the_code_nor_a_secret(pem):
    github, security = FakeGitHub(pem, status=404), FakeSecurity()
    flow = flow_for(transport=github, run=security)
    status, _, body = flow.handle("GET", f"/callback?code={CODE}&state=the-state")
    assert status == 500 and "GitHub said 404" in flow.error
    assert security.calls == [] and flow.result is None
    assert_nothing_secret(flow.error + body.decode(), pem)


def test_when_the_keychain_refuses_it_says_how_to_recover_without_the_key(pem):
    security = FakeSecurity(returncode=1, stderr="security: SecKeychainItemCreateFromContent: error")
    flow = flow_for(transport=FakeGitHub(pem), run=security)
    assert flow.handle("GET", f"/callback?code={CODE}&state=the-state")[0] == 500
    assert "Keychain refused basita-gh-talia-app" in flow.error
    assert "settings/apps/basita-talia" in flow.error and "gh-store talia" in flow.error
    assert_nothing_secret(flow.error, pem)


def test_an_unexpected_failure_shows_only_its_type(pem):
    def broken(cmd, **_):
        raise RuntimeError(f"boom {pem}")

    flow = flow_for(transport=FakeGitHub(pem), run=broken)
    assert flow.handle("GET", f"/callback?code={CODE}&state=the-state")[0] == 500
    assert "unexpected RuntimeError" in flow.error and "gh-store talia" in flow.error
    assert_nothing_secret(flow.error, pem)


def test_store_key_refuses_what_is_not_a_private_key():
    security = FakeSecurity()
    assert app_new.store_key("basita-gh-talia-app", "not a key", security) is False
    assert security.calls == []


# --- the whole run, through the local server ---------------------------------------------------


def _get(url):
    parts = urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    connection.request("GET", parts.path + (f"?{parts.query}" if parts.query else ""))
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read().decode()
    connection.close()
    return result


def test_the_whole_run_on_127_0_0_1_with_the_browser_played_by_the_test(pem, capsys):
    github, security, seen = FakeGitHub(pem), FakeSecurity(), {}

    def browser(local):
        def visit():
            seen["local"] = local
            _, _, page = _get(local)
            action = html.unescape(re.search(r"action='([^']+)'", page).group(1))
            manifest = json.loads(html.unescape(re.search(r"value='([^']+)'", page).group(1)))
            state = action.split("state=", 1)[1]
            seen["callback"] = _get(f"{manifest['redirect_url']}?code={CODE}&state={state}")

        seen["thread"] = threading.Thread(target=visit, daemon=True)
        seen["thread"].start()

    assert app_new.main(["talia"], config=CONFIG, transport=github, run=security, opener=browser, timeout=20) == 0
    seen["thread"].join(5)
    assert urlsplit(seen["local"]).hostname == "127.0.0.1"
    status, headers, _ = seen["callback"]
    assert status == 302 and headers["Location"].endswith("/apps/basita-talia/installations/new")
    assert len(security.stored()) == 1
    captured = capsys.readouterr()
    assert "created basita-talia: app id 7000001" in captured.out
    assert '"app_id": 7000001,' in captured.out
    assert_nothing_secret(captured.out + captured.err, pem)


def test_the_run_gives_up_after_the_timeout_and_creates_nothing(pem, capsys):
    github, security = FakeGitHub(pem), FakeSecurity()
    assert app_new.main(["talia"], config=CONFIG, transport=github, run=security, opener=lambda _: None, timeout=1) == 1
    assert "no answer from GitHub" in capsys.readouterr().out
    assert github.calls == [] and security.calls == []


def test_a_wrong_state_through_the_server_ends_the_run_with_a_refusal(pem, capsys):
    github, security = FakeGitHub(pem), FakeSecurity()

    def browser(local):
        parts = urlsplit(local)
        threading.Thread(
            target=_get, args=(f"http://127.0.0.1:{parts.port}/callback?code={CODE}&state=forged",), daemon=True
        ).start()

    assert app_new.main(["talia"], config=CONFIG, transport=github, run=security, opener=browser, timeout=20) == 1
    assert "not this run's" in capsys.readouterr().err
    assert github.calls == [] and security.calls == []
