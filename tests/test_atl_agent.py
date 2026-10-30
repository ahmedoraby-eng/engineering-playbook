import base64
import json
import subprocess

import pytest

from atl_agent import AtlClient, AtlError, keychain_token
from atl_agent.__main__ import main

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        return self.responses.pop(0)


def client(transport, agent="salma"):
    return AtlClient(agent, CLOUD, TOKEN, transport=transport)


def test_jira_comment_posts_adf_through_the_gateway_as_the_agent():
    transport = FakeTransport((201, json.dumps({"id": "10", "author": {"displayName": "Salma"}})))
    result = client(transport).jira_comment("BASITA-1", "line one\n\nline three")
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/issue/BASITA-1/comment"
    assert base64.b64decode(headers["Authorization"].split()[1]).decode() == f"salma@basitasolutions.com:{TOKEN}"
    assert body["body"]["type"] == "doc"
    assert [p.get("content", [{}])[0].get("text") for p in body["body"]["content"]] == ["line one", None, "line three"]
    assert result["author"]["displayName"] == "Salma"


def test_confluence_append_bumps_the_version_and_names_the_agent():
    page = {"id": "77", "title": "T", "version": {"number": 4}, "body": {"storage": {"value": "<p>old</p>"}}}
    transport = FakeTransport((200, json.dumps(page)), (200, json.dumps({"version": {"number": 5}})))
    client(transport).confluence_append("77", "new <b>")
    get, put = transport.calls
    assert get[0] == "GET" and get[1].endswith("/ex/confluence/cloud-1/wiki/api/v2/pages/77?body-format=storage")
    assert put[0] == "PUT"
    assert put[3]["body"]["value"] == "<p>old</p><p>new &lt;b&gt;</p>"
    assert put[3]["version"] == {"number": 5, "message": "[Salma] new <b>"}


def test_errors_explain_the_status_and_never_contain_the_token():
    transport = FakeTransport((403, '{"message":"scope does not match"}'))
    with pytest.raises(AtlError) as caught:
        client(transport).jira_comment("BASITA-1", "x")
    message = str(caught.value)
    assert "HTTP 403" in message and "scopes" in message
    assert TOKEN not in message and TOKEN not in repr(client(transport))


def test_unknown_agent_and_missing_cloud_id_are_refused():
    with pytest.raises(AtlError, match="unknown agent"):
        AtlClient("mallory", CLOUD, TOKEN)
    with pytest.raises(AtlError, match="cloud id"):
        AtlClient("salma", "", TOKEN)


def test_keychain_token_prefers_the_product_item_then_falls_back():
    seen = []

    def run(cmd, **_):
        seen.append(cmd[3])
        found = cmd[3] == "basita-atl-salma"
        return subprocess.CompletedProcess(cmd, 0 if found else 44, stdout=TOKEN + "\n" if found else "", stderr="")

    assert keychain_token("salma", "jira", run=run) == TOKEN
    assert seen == ["basita-atl-salma-jira", "basita-atl-salma"]


def test_keychain_token_reads_the_product_item():
    def run(cmd, **_):
        return subprocess.CompletedProcess(cmd, 0, stdout=TOKEN, stderr="")

    assert keychain_token("salma", "confluence", run=run) == TOKEN


def test_keychain_token_missing_item_is_an_error():
    def run(cmd, **_):
        return subprocess.CompletedProcess(cmd, 44, stdout="", stderr="not found")

    with pytest.raises(AtlError, match="basita-atl-salma-jira or basita-atl-salma"):
        keychain_token("salma", "jira", run=run)
    with pytest.raises(AtlError, match="no Keychain item basita-atl-salma "):
        keychain_token("salma", run=run)


def test_cli_posts_a_comment(capsys):
    transport = FakeTransport((201, json.dumps({"id": "10", "author": {"displayName": "Salma"}})))
    code = main(
        ["Salma", "jira-comment", "BASITA-1", "hello", "--cloud-id", CLOUD],
        token_source=lambda agent, product: TOKEN,
        transport=transport,
    )
    assert code == 0
    assert "comment 10 on BASITA-1 by Salma" in capsys.readouterr().out


def test_cli_appends_to_a_page(capsys):
    page = {"id": "77", "title": "T", "version": {"number": 1}, "body": {"storage": {"value": ""}}}
    transport = FakeTransport((200, json.dumps(page)), (200, json.dumps({"version": {"number": 2}})))
    code = main(
        ["salma", "confluence-append", "77", "hi", "--cloud-id", CLOUD],
        token_source=lambda agent, product: TOKEN,
        transport=transport,
    )
    assert code == 0
    assert "page 77 now version 2 by Salma" in capsys.readouterr().out


def test_cli_reports_errors_without_the_token(capsys):
    def no_token(agent, product):
        raise AtlError("no Keychain item 'basita-atl-salma'")

    assert main(["salma", "jira-comment", "BASITA-1", "x", "--cloud-id", CLOUD], token_source=no_token) == 1
    assert "basita-atl-salma" in capsys.readouterr().err


def test_cli_rejects_empty_text(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("  "))
    assert main(["salma", "jira-comment", "BASITA-1", "-", "--cloud-id", CLOUD], token_source=lambda a, p: TOKEN) == 2


def test_cloud_id_defaults_to_basitas_site_and_can_be_overridden():
    from atl_agent.client import DEFAULT_CLOUD_ID, default_cloud_id

    assert default_cloud_id({}) == DEFAULT_CLOUD_ID
    assert default_cloud_id({"ATL_CLOUD_ID": "  "}) == DEFAULT_CLOUD_ID
    assert default_cloud_id({"ATL_CLOUD_ID": "other"}) == "other"


def test_cli_works_without_cloud_id_in_the_environment(monkeypatch):
    from atl_agent.client import DEFAULT_CLOUD_ID

    monkeypatch.delenv("ATL_CLOUD_ID", raising=False)
    transport = FakeTransport((201, json.dumps({"id": "1", "author": {"displayName": "Salma"}})))
    assert main(["salma", "jira-comment", "BASITA-1", "hi"], token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert DEFAULT_CLOUD_ID in transport.calls[0][1]
