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


def _issue(status, kind="Task"):
    return (200, json.dumps({"fields": {"status": {"name": status}, "issuetype": {"name": kind}}}))


def _moves(*names):
    return (200, json.dumps({"transitions": [{"id": str(i), "to": {"name": n}} for i, n in enumerate(names, 1)]}))


def test_jira_ready_takes_only_the_refinement_to_ready_transition():
    transport = FakeTransport(_issue("Refinement"), _moves("Done", "Ready", "In Progress"), (204, ""))
    assert client(transport).jira_ready("BASITA-5") == "Ready"
    method, url, _, body = transport.calls[-1]
    assert method == "POST" and url.endswith("/issue/BASITA-5/transitions") and body == {"transition": {"id": "2"}}


@pytest.mark.parametrize("status,kind", [("To Do", "Task"), ("In Progress", "Task"), ("Refinement", "Epic")])
def test_jira_ready_refuses_anything_but_a_refinement_task(status, kind):
    transport = FakeTransport(_issue(status, kind))
    with pytest.raises(AtlError):
        client(transport).jira_ready("BASITA-5")
    assert len(transport.calls) == 1


def test_jira_ready_refuses_when_no_transition_lands_on_ready():
    transport = FakeTransport(_issue("Refinement"), _moves("Done", "In Progress"))
    with pytest.raises(AtlError, match="no transition to Ready"):
        client(transport).jira_ready("BASITA-5")
    assert len(transport.calls) == 2


def test_cli_jira_ready_transitions_and_comments(capsys):
    transport = FakeTransport(_issue("Refinement"), _moves("Ready"), (204, ""), (201, json.dumps({"id": "3"})))
    argv = ["haya", "jira-ready", "BASITA-5", "--cloud-id", CLOUD]
    assert main(argv, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "BASITA-5 -> Ready, by Haya" in capsys.readouterr().out


def test_jira_ready_lands_on_ready_for_development_in_dfp():
    transport = FakeTransport(_issue("Refinement"), _moves("In Development", "Ready for Development"), (204, ""))
    assert client(transport, "adam").jira_ready("DFP-88") == "Ready for Development"
    assert transport.calls[-1][3] == {"transition": {"id": "2"}}


def test_jira_ready_in_a_project_without_a_ready_status_changes_nothing():
    transport = FakeTransport(_issue("Refinement"), _moves("To Do", "Done"))
    with pytest.raises(AtlError, match="no transition to Ready"):
        client(transport).jira_ready("BASITA-5")
    assert [c[0] for c in transport.calls] == ["GET", "GET"]


# ---- BASITA-38: links, Salma's queue, Adam's return path, Salma's closing (fake transport; the DFP workflow
# statuses Ready for Testing / In Testing / Closed do not exist in Jira yet) ----


def _status_issue(status, category="new", labels=()):
    return (
        200,
        json.dumps(
            {"fields": {"status": {"name": status, "statusCategory": {"key": category}}, "labels": list(labels)}}
        ),
    )


def _to(*pairs):
    return (
        200,
        json.dumps(
            {
                "transitions": [
                    {"id": str(i), "to": {"name": n, "statusCategory": {"key": c}}} for i, (n, c) in enumerate(pairs, 1)
                ]
            }
        ),
    )


def test_comment_links_are_clickable_in_adf():
    from atl_agent.client import _adf

    text = "PR [basita-ops#9](https://github.com/o/r/pull/9) and https://example.com/a?b=1, done."
    nodes = _adf(text)["content"][0]["content"]
    links = [n for n in nodes if n.get("marks")]
    assert [(n["text"], n["marks"][0]["attrs"]["href"]) for n in links] == [
        ("basita-ops#9", "https://github.com/o/r/pull/9"),
        ("https://example.com/a?b=1", "https://example.com/a?b=1"),
    ]
    assert "".join(n["text"] for n in nodes) == "PR basita-ops#9 and https://example.com/a?b=1, done."


def test_plain_text_without_links_is_unchanged():
    from atl_agent.client import _adf

    assert _adf("no links here")["content"][0]["content"] == [{"type": "text", "text": "no links here"}]


def test_salma_pulls_from_ready_for_testing_into_in_testing():
    transport = FakeTransport(
        _status_issue("Ready for Testing", "indeterminate"),
        _to(("In Development", "indeterminate"), ("In Testing", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert client(transport, "salma").jira_start("DFP-5") == "In Testing"
    assert transport.calls[-1][3] == {"transition": {"id": "2"}}


def test_no_other_agent_pulls_from_salmas_queue():
    transport = FakeTransport(_status_issue("Ready for Testing"))
    with pytest.raises(AtlError, match="Salma's queue"):
        client(transport, "malek").jira_start("DFP-5")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", ["Ready for Development", "Refinement", "In Testing"])
def test_salma_pulls_nothing_else_in_dfp(status):
    transport = FakeTransport(_status_issue(status))
    with pytest.raises(AtlError, match="pulls only from 'Ready for Testing'"):
        client(transport, "salma").jira_start("DFP-5")
    assert len(transport.calls) == 1


def test_salma_still_starts_to_do_issues_outside_dfp():
    transport = FakeTransport(_status_issue("To Do"), _to(("In Progress", "indeterminate")), (204, ""), (204, ""))
    assert client(transport, "salma").jira_start("BASITA-9") == "In Progress"


def test_engineers_still_go_to_in_development():
    transport = FakeTransport(
        _status_issue("Ready for Development"),
        _to(("In Testing", "indeterminate"), ("In Development", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert client(transport, "malek").jira_start("DFP-5") == "In Development"  # never In Testing: that is Salma's


def test_adam_returns_from_refinement_to_ready_for_testing_assigned_to_salma():
    transport = FakeTransport(
        _issue("Refinement"), _moves("Ready for Development", "Ready for Testing"), (204, ""), (204, "")
    )
    assert client(transport, "adam").jira_return("DFP-7") == "Ready for Testing"
    assert transport.calls[-2][3] == {"transition": {"id": "2"}}
    method, url, _, body = transport.calls[-1]
    assert (
        method == "PUT"
        and url.endswith("/issue/DFP-7/assignee")
        and body == {"accountId": "712020:a11d40c9-5c21-4caf-8743-b118b75cd9d2"}
    )


@pytest.mark.parametrize(
    "agent,status,kind",
    [("malek", "Refinement", "Task"), ("adam", "In Development", "Task"), ("adam", "Refinement", "Epic")],
)
def test_jira_return_is_adams_and_only_leaves_refinement(agent, status, kind):
    transport = FakeTransport(_issue(status, kind))
    with pytest.raises(AtlError):
        client(transport, agent).jira_return("DFP-7")
    assert all(c[0] == "GET" for c in transport.calls)


def test_cli_jira_return_needs_the_reason_and_comments_it(capsys):
    argv = ["adam", "jira-return", "DFP-7"]
    assert main([*argv, "--cloud-id", CLOUD], token_source=lambda a, p: TOKEN, transport=FakeTransport()) == 2
    transport = FakeTransport(
        _issue("Refinement"), _moves("Ready for Testing"), (204, ""), (204, ""), (201, json.dumps({"id": "9"}))
    )
    assert (
        main(
            [*argv, "Not a bug: works as specified", "--cloud-id", CLOUD],
            token_source=lambda a, p: TOKEN,
            transport=transport,
        )
        == 0
    )
    assert "Not a bug: works as specified" in json.dumps(transport.calls[-1][3])
    assert "DFP-7 -> Ready for Testing, assigned to Salma" in capsys.readouterr().out


def test_salma_closes_from_in_testing_with_a_reason():
    transport = FakeTransport(_issue("In Testing"), _moves("Ready for Testing", "Closed"), (204, ""))
    assert client(transport, "salma").jira_dismiss("DFP-7", "Duplicate") == "Closed"
    assert transport.calls[-1][3] == {"transition": {"id": "2"}}


@pytest.mark.parametrize(
    "agent,status,reason",
    [
        ("adam", "In Testing", "Duplicate"),
        ("salma", "Ready for Testing", "Duplicate"),
        ("salma", "In Testing", "Because"),
    ],
)
def test_jira_dismiss_is_salmas_from_in_testing_with_a_listed_reason(agent, status, reason):
    transport = FakeTransport(_issue(status))
    with pytest.raises(AtlError):
        client(transport, agent).jira_dismiss("DFP-7", reason)
    assert all(c[0] == "GET" for c in transport.calls)


def test_cli_jira_dismiss_requires_a_reason_and_comments_it(capsys):
    argv = ["salma", "jira-dismiss", "DFP-7", "--cloud-id", CLOUD]
    assert main(argv, token_source=lambda a, p: TOKEN, transport=FakeTransport()) == 2
    transport = FakeTransport(_issue("In Testing"), _moves("Closed"), (204, ""), (201, json.dumps({"id": "9"})))
    assert main([*argv, "--reason", "Already Fixed"], token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "Closed: Already Fixed." in json.dumps(transport.calls[-1][3])
    assert "DFP-7 -> Closed (Already Fixed)" in capsys.readouterr().out
