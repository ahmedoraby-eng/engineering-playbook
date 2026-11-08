import base64
import io
import json
import subprocess
from pathlib import Path

import pytest

from atl_agent import AtlClient, AtlError, keychain_token
from atl_agent.__main__ import main
from atl_agent.client import ADAM_ACCOUNT, AHMED_ACCOUNT, JIRA_ACCOUNTS, SALMA_ACCOUNT, _adf, _adf_text

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"
MALEK_ID = "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"
HAYA_ID = "712020:dd45049f-7336-4c37-9f99-e8665102ed92"


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
    monkeypatch.setattr("sys.stdin", io.StringIO("  "))
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


def _refining(versions=({"id": "2", "released": False},), status="Refinement"):
    fields = {"status": {"name": status}, "issuetype": {"name": "Task"}, "fixVersions": list(versions)}
    return (200, json.dumps({"fields": fields}))


@pytest.mark.parametrize(
    "name, account",
    [("malek", MALEK_ID), ("Haya", HAYA_ID), ("sherif", JIRA_ACCOUNTS["sherif"]), ("ahmed", AHMED_ACCOUNT)],
)
def test_jira_ready_in_dfp_assigns_then_moves(name, account):
    transport = FakeTransport(_refining(), _moves("In Development", "Ready for Development"), (204, ""), (204, ""))
    assert client(transport, "adam").jira_ready("DFP-88", name) == "Ready for Development"
    assert transport.calls[2][0] == "PUT" and transport.calls[2][1].endswith("/assignee")
    assert transport.calls[2][3] == {"accountId": account}
    assert transport.calls[3][3] == {"transition": {"id": "2"}}


def test_jira_ready_in_dfp_needs_a_name_and_changes_nothing():
    transport = FakeTransport(_refining())
    with pytest.raises(AtlError, match="--assignee"):
        client(transport, "adam").jira_ready("DFP-88")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("name", ["adam", "Salma"])
def test_jira_ready_refuses_who_pickup_would_not_start(name):
    transport = FakeTransport(_refining())
    with pytest.raises(AtlError, match="never starts"):
        client(transport, "adam").jira_ready("DFP-88", name)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("name", ["maya", "basel", "talia", "mallory"])
def test_jira_ready_refuses_a_name_with_no_seat(name):
    transport = FakeTransport(_refining())
    with pytest.raises(AtlError, match="no Jira seat"):
        client(transport, "adam").jira_ready("DFP-88", name)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("versions", [(), ({"id": "1", "released": True},)])
def test_jira_ready_with_no_unreleased_version_stays_and_says_who_acts(versions):
    transport = FakeTransport(_refining(versions))
    with pytest.raises(AtlError) as error:
        client(transport, "adam").jira_ready("DFP-88", "malek")
    assert "no unreleased Fix Version" in str(error.value) and "assign it to Adam again" in str(error.value)
    assert len(transport.calls) == 1


def test_jira_ready_run_again_after_a_failed_move_finishes():
    failing = FakeTransport(_refining(), _moves("Ready for Development"), (204, ""), (500, "{}"))
    with pytest.raises(AtlError):
        client(failing, "adam").jira_ready("DFP-88", "malek")
    again = FakeTransport(_refining(), _moves("Ready for Development"), (204, ""), (204, ""))
    assert client(again, "adam").jira_ready("DFP-88", "malek") == "Ready for Development"


def test_jira_ready_elsewhere_needs_no_name_or_version():
    transport = FakeTransport(_issue("Refinement"), _moves("Ready"), (204, ""))
    assert client(transport, "adam").jira_ready("ELB-1") == "Ready"
    assert len(transport.calls) == 3


def test_jira_ready_elsewhere_can_name_one():
    transport = FakeTransport(_issue("Refinement"), _moves("Ready"), (204, ""), (204, ""))
    assert client(transport, "adam").jira_ready("BASITA-1", "malek") == "Ready"
    assert transport.calls[2][3] == {"accountId": MALEK_ID}


def test_cli_jira_ready_assigns_and_says_so(capsys):
    transport = FakeTransport(
        _refining(), _moves("Ready for Development"), (204, ""), (204, ""), (201, json.dumps({"id": "5"}))
    )
    args = ["adam", "jira-ready", "DFP-88", "Refined.", "--assignee", "malek", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "DFP-88 -> Ready for Development, assigned to Malek, by Adam" in capsys.readouterr().out


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
    assert transport.calls[-2][3] == {"transition": {"id": "2"}}


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


@pytest.mark.parametrize("agent", ["salma", "adam", "malek"])
def test_every_agent_pulls_a_simple_workflow_to_do_issue_into_in_progress(agent):
    """ELB and BASITA only have To Do / In Progress / Done: no refinement or testing-queue rules there."""
    transport = FakeTransport(_status_issue("To Do"), _to(("In Progress", "indeterminate")), (204, ""), (204, ""))
    assert client(transport, agent).jira_start("ELB-9") == "In Progress"


def test_a_failed_move_leaves_no_label_on_the_queued_issue():
    transport = FakeTransport(
        _status_issue("To Do"), _to(("In Progress", "indeterminate")), (403, '{"errorMessages":["no"]}')
    )
    with pytest.raises(AtlError):
        client(transport, "malek").jira_start("ELB-9")
    assert [c[0] for c in transport.calls] == ["GET", "GET", "POST"]


def test_a_failed_label_says_the_issue_already_moved():
    transport = FakeTransport(
        _status_issue("To Do"), _to(("In Progress", "indeterminate")), (204, ""), (403, '{"errorMessages":["no"]}')
    )
    with pytest.raises(AtlError, match="moved to 'In Progress'.*label failed"):
        client(transport, "malek").jira_start("ELB-9")


def _issue_with_assignee(status, assignee, category="new"):
    fields = {"status": {"name": status, "statusCategory": {"key": category}}, "labels": [], "assignee": assignee}
    return (200, json.dumps({"fields": fields}))


def test_start_assigns_an_unassigned_issue_to_the_agent():
    transport = FakeTransport(
        _issue_with_assignee("To Do", None), _to(("In Progress", "indeterminate")), (204, ""), (204, ""), (204, "")
    )
    assert client(transport, "malek").jira_start("BASITA-216") == "In Progress"
    assert "assignee" in transport.calls[0][1]
    method, url, _, body = transport.calls[-1]
    assert method == "PUT" and url.endswith("/rest/api/3/issue/BASITA-216/assignee")
    assert body == {"accountId": JIRA_ACCOUNTS["malek"]}


def test_start_keeps_an_existing_assignee():
    transport = FakeTransport(
        _issue_with_assignee("To Do", {"accountId": "someone"}),
        _to(("In Progress", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert client(transport, "malek").jira_start("BASITA-216") == "In Progress"
    assert not any(url.endswith("/assignee") for _, url, _, _ in transport.calls)


def test_start_assigns_a_routed_agent_through_its_focal_user():
    transport = FakeTransport(
        _issue_with_assignee("To Do", None), _to(("In Progress", "indeterminate")), (204, ""), (204, ""), (204, "")
    )
    AtlClient("maya", CLOUD, TOKEN, transport=transport, via="sherif").jira_start("BASITA-9")
    assert transport.calls[-1][3] == {"accountId": JIRA_ACCOUNTS["sherif"]}


def test_a_failed_assignment_says_the_issue_already_moved():
    transport = FakeTransport(
        _issue_with_assignee("To Do", None),
        _to(("In Progress", "indeterminate")),
        (204, ""),
        (204, ""),
        (403, '{"errorMessages":["no"]}'),
    )
    with pytest.raises(AtlError, match="moved to 'In Progress' and labelled, but assigning it failed"):
        client(transport, "malek").jira_start("BASITA-216")


def test_a_handoff_in_a_queue_starts_although_the_previous_agent_left_the_label():
    transport = FakeTransport(
        _status_issue("Ready for Testing", "indeterminate", ["agent-picked"]),
        _to(("In Testing", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert client(transport, "salma").jira_start("DFP-5") == "In Testing"


def test_a_handoff_through_another_user_starts_too():
    transport = FakeTransport(
        _status_issue("Ready for Development", "new", ["agent-picked"]),
        _to(("In Development", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert AtlClient("maya", CLOUD, TOKEN, transport=transport, via="sherif").jira_start("DFP-7") == "In Development"


def test_the_label_still_stops_a_second_pickup_outside_a_queue():
    transport = FakeTransport(_status_issue("To Do", "new", ["agent-picked"]))
    with pytest.raises(AtlError, match="already has"):
        client(transport, "malek").jira_start("BASITA-5")


def _read_responses():
    fields = {
        "summary": "S",
        "status": {"name": "To Do"},
        "issuetype": {"name": "Task"},
        "labels": ["l"],
        "assignee": {"displayName": "Malek"},
        "reporter": {"displayName": "Ahmed"},
        "priority": {"name": "High"},
        "parent": {"key": "BASITA-2"},
        "description": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Given"}]},
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "see",
                                            "marks": [{"type": "link", "attrs": {"href": "https://x.test"}}],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
            ],
        },
        "subtasks": [{"key": "BASITA-9", "fields": {"summary": "sub", "status": {"name": "Done"}}}],
        "issuelinks": [
            {
                "type": {"outward": "blocks", "inward": "is blocked by"},
                "outwardIssue": {"key": "DFP-1", "fields": {"summary": "A", "status": {"name": "Done"}}},
            },
            {
                "type": {"outward": "blocks", "inward": "is blocked by"},
                "inwardIssue": {"key": "DFP-2", "fields": {"summary": "B", "status": {"name": "To Do"}}},
            },
        ],
    }
    comment = {
        "author": {"displayName": "Haya"},
        "created": "2026-10-31",
        "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]},
    }
    return (
        (200, json.dumps({"fields": fields})),
        (200, json.dumps({"total": 1, "comments": [comment]})),
        (200, json.dumps({"issues": [{"key": "BASITA-1"}]})),
    )


def test_jira_get_returns_the_issue_comments_and_links_with_get_calls_only():
    transport = FakeTransport(*_read_responses())
    result = client(transport, "malek").jira_get("BASITA-1")
    assert {call[0] for call in transport.calls} == {"GET"}
    auth = transport.calls[0][2]["Authorization"]
    assert base64.b64decode(auth.split()[1]).decode() == f"malek@basitasolutions.com:{TOKEN}"
    assert "currentUser" in transport.calls[2][1]
    assert result["read_by"] == "Malek" and result["assigned_to_reader"] is True
    assert result["status"] == "To Do" and result["parent"] == "BASITA-2" and result["labels"] == ["l"]
    assert result["description"] == "Given\n- see (https://x.test)"
    assert result["comments"] == [{"author": "Haya", "created": "2026-10-31", "text": "hi"}]
    assert result["subtasks"] == [{"key": "BASITA-9", "summary": "sub", "status": "Done"}]
    assert result["links"] == [
        {"relation": "blocks", "key": "DFP-1", "summary": "A", "status": "Done"},
        {"relation": "is blocked by", "key": "DFP-2", "summary": "B", "status": "To Do"},
    ]


def test_jira_get_follows_comment_pages():
    issue, _, mine = _read_responses()
    one = {"author": {"displayName": "H"}, "created": "d", "body": None}
    transport = FakeTransport(
        issue,
        (200, json.dumps({"total": 2, "comments": [one]})),
        (200, json.dumps({"total": 2, "comments": [one]})),
        mine,
    )
    assert len(client(transport).jira_get("BASITA-1")["comments"]) == 2
    assert "startAt=1" in transport.calls[2][1]


def test_jira_get_refuses_anything_but_an_issue_key():
    with pytest.raises(AtlError, match="not an issue key"):
        client(FakeTransport()).jira_get("BASITA-1/../../x")


def test_cli_jira_get_prints_json_and_needs_no_text(capsys):
    transport = FakeTransport(*_read_responses())
    args = ["malek", "jira-get", "BASITA-1", "--json", "--cloud-id", CLOUD]
    code = main(args, token_source=lambda a, p: TOKEN, transport=transport)
    assert code == 0
    assert json.loads(capsys.readouterr().out)["key"] == "BASITA-1"


def test_jira_jql_follows_next_page_tokens_and_stops_at_the_limit():
    def issue(key):
        return {"key": key, "fields": {"summary": key, "status": {"name": "To Do"}, "issuetype": {"name": "Task"}}}

    transport = FakeTransport(
        (200, json.dumps({"issues": [issue("DFP-1")], "nextPageToken": "t2"})),
        (200, json.dumps({"issues": [issue("DFP-2")]})),
    )
    result = client(transport).jira_jql("project = DFP", limit=5)
    assert [i["key"] for i in result["issues"]] == ["DFP-1", "DFP-2"] and result["count"] == 2
    assert "jql=project%20%3D%20DFP" in transport.calls[0][1] and "nextPageToken=t2" in transport.calls[1][1]
    assert result["read_by"] == "Salma"


def test_cli_read_commands_use_the_jira_token(capsys):
    seen = []

    def token(agent, product):
        seen.append(product)
        return TOKEN

    transport = FakeTransport((200, json.dumps({"issues": []})))
    assert (
        main(["haya", "jira-jql", "project = DFP", "--cloud-id", CLOUD], token_source=token, transport=transport) == 0
    )
    assert seen == ["jira"] and json.loads(capsys.readouterr().out)["read_by"] == "Haya"


def test_adf_text_keeps_table_rows_on_one_line():
    def cell(text):
        return {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}

    table = {"type": "table", "content": [{"type": "tableRow", "content": [cell("Owner"), cell("Ahmed")]}]}
    assert _adf_text(table) == "Owner | Ahmed"


def _doc(text):
    content = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
    return {"atlas_doc_format": {"value": json.dumps(content)}}


def _page_responses():
    page = {
        "id": "123",
        "title": "Plan",
        "status": "current",
        "spaceId": "7",
        "parentId": "100",
        "version": {"number": 3, "createdAt": "2026-10-31"},
        "body": _doc("Body"),
        "labels": {"results": [{"name": "elb"}]},
        "_links": {"base": "https://x.atlassian.net/wiki", "webui": "/spaces/ELB/pages/123"},
    }
    return (
        (200, json.dumps(page)),
        (200, json.dumps({"key": "ELB", "name": "Elbayoumy"})),
        (200, json.dumps({"results": [{"id": "9", "body": _doc("footer"), "version": {"createdAt": "d1"}}]})),
        (200, json.dumps({"results": [{"id": "8", "body": _doc("inline"), "version": {"createdAt": "d2"}}]})),
        (200, json.dumps({"results": [{"title": "a.pdf", "fileSize": 5}]})),
        (200, json.dumps({"results": [{"id": "124", "title": "Child", "type": "page"}]})),
    )


def test_confluence_get_takes_an_id_or_a_url_and_only_reads():
    transport = FakeTransport(*_page_responses())
    result = client(transport).confluence_get("https://x.atlassian.net/wiki/spaces/ELB/pages/123/Plan")
    assert transport.calls[0][1].endswith("/wiki/api/v2/pages/123?body-format=atlas_doc_format&include-labels=true")
    assert {call[0] for call in transport.calls} == {"GET"}
    assert all("/ex/confluence/cloud-1/" in call[1] for call in transport.calls)
    assert result["read_by"] == "Salma" and result["title"] == "Plan" and result["space"] == "ELB"
    assert result["body"] == "Body" and result["labels"] == ["elb"] and result["version"] == 3
    assert [c["text"] for c in result["comments"]] == ["footer", "inline"]
    assert result["attachments"] == [{"title": "a.pdf", "size": 5}]
    assert result["children"] == [{"id": "124", "title": "Child", "type": "page"}]
    assert result["url"] == "https://x.atlassian.net/wiki/spaces/ELB/pages/123"


def test_confluence_lists_follow_next_links():
    transport = FakeTransport(
        (200, json.dumps({"results": [{"id": "1"}], "_links": {"next": "/wiki/api/v2/x?cursor=b"}})),
        (200, json.dumps({"results": [{"id": "2"}]})),
    )
    assert [r["id"] for r in client(transport)._results("/wiki/api/v2/x")] == ["1", "2"]
    assert transport.calls[1][1].endswith("/wiki/api/v2/x?cursor=b")


def test_confluence_get_with_storage_also_gives_the_storage_body_and_only_reads():
    """BASITA-191: Sherif edits the cost log's tables exactly, then writes them back with confluence-edit --storage."""
    storage = "<table><tr><td>03/11</td></tr></table><p>x &amp; y</p>"
    transport = FakeTransport(*_page_responses(), (200, json.dumps({"body": {"storage": {"value": storage}}})))
    result = client(transport).confluence_get("123", storage=True)
    assert result["storage"] == storage and result["body"] == "Body"
    assert transport.calls[-1][1].endswith("/wiki/api/v2/pages/123?body-format=storage")
    assert {call[0] for call in transport.calls} == {"GET"}


def test_confluence_get_without_storage_reads_no_storage():
    transport = FakeTransport(*_page_responses())
    assert "storage" not in client(transport).confluence_get("123")
    assert not any("body-format=storage" in call[1] for call in transport.calls)


def test_cli_storage_is_for_confluence_get_not_the_other_reads(capsys):
    for command in ("jira-get", "jira-jql", "confluence-search"):
        transport = FakeTransport()
        assert (
            main(
                ["salma", command, "DFP-1", "--storage", "--cloud-id", CLOUD],
                token_source=lambda a, p: TOKEN,
                transport=transport,
            )
            == 2
        )
        assert transport.calls == []
    assert "--storage is for confluence-get" in capsys.readouterr().err


def test_cli_confluence_get_storage(capsys):
    transport = FakeTransport(*_page_responses(), (200, json.dumps({"body": {"storage": {"value": "<p>s</p>"}}})))
    args = ["sherif", "confluence-get", "123", "--storage", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert json.loads(capsys.readouterr().out)["storage"] == "<p>s</p>"


def test_confluence_get_refuses_what_is_not_a_page():
    with pytest.raises(AtlError, match="not a Confluence page"):
        client(FakeTransport()).confluence_get("plan")


def test_confluence_search_takes_words_or_cql():
    hit = {
        "content": {"id": "5", "type": "page"},
        "title": "Rations",
        "excerpt": "the @@@hl@@@ration@@@endhl@@@ plan",
        "url": "/spaces/ELB/pages/5",
        "resultGlobalContainer": {"title": "Elbayoumy"},
    }
    transport = FakeTransport((200, json.dumps({"results": [hit], "_links": {"base": "https://x/wiki"}})))
    result = client(transport).confluence_search("ration plan")
    assert result["cql"] == 'type in (page, blogpost) AND text ~ "ration plan"'
    assert result["results"][0]["excerpt"] == "the ration plan"
    assert result["results"][0]["url"] == "https://x/wiki/spaces/ELB/pages/5"
    transport = FakeTransport((200, json.dumps({"results": []})))
    cql = 'space = ELB AND title ~ "plan"'
    assert client(transport).confluence_search(cql)["cql"] == cql


def test_confluence_create_finds_the_space_and_publishes_as_the_agent():
    transport = FakeTransport(
        (200, json.dumps({"results": [{"id": "7"}]})),
        (200, json.dumps({"id": "200", "version": {"number": 1}})),
    )
    client(transport, "maya").confluence_create("ELB", "Analysis", "line [x](https://x.test)", parent="100")
    assert transport.calls[0][1].endswith("/wiki/api/v2/spaces?keys=ELB")
    method, url, headers, body = transport.calls[1]
    assert method == "POST" and url.endswith("/wiki/api/v2/pages")
    assert base64.b64decode(headers["Authorization"].split()[1]).decode().startswith("maya@")
    assert body["spaceId"] == "7" and body["title"] == "Analysis" and body["status"] == "current"
    assert body["parentId"] == "100" and body["body"]["representation"] == "atlas_doc_format"
    assert "https://x.test" in body["body"]["value"]


def test_confluence_create_refuses_an_unknown_space():
    with pytest.raises(AtlError, match="no space"):
        client(FakeTransport((200, json.dumps({"results": []})))).confluence_create("NOPE", "T", "x")


def test_confluence_edit_bumps_the_version_and_names_the_agent():
    page = {"id": "123", "title": "Plan", "version": {"number": 4}, "body": {"storage": {"value": "<p>old</p>"}}}
    transport = FakeTransport((200, json.dumps(page)), (200, json.dumps({"id": "123", "version": {"number": 5}})))
    client(transport).confluence_edit("123", "new text")
    method, url, _, body = transport.calls[1]
    assert method == "PUT" and url.endswith("/wiki/api/v2/pages/123")
    assert body["version"] == {"number": 5, "message": "[Salma] new text"}
    assert body["title"] == "Plan" and body["body"]["representation"] == "atlas_doc_format"


def test_confluence_edit_of_the_title_keeps_the_body():
    page = {"id": "123", "title": "Plan", "version": {"number": 4}, "body": {"storage": {"value": "<p>old</p>"}}}
    transport = FakeTransport((200, json.dumps(page)), (200, json.dumps({"id": "123"})))
    client(transport).confluence_edit("123", title="New")
    body = transport.calls[1][3]
    assert body["title"] == "New" and body["body"] == {"representation": "storage", "value": "<p>old</p>"}


def test_confluence_edit_with_nothing_to_change_is_refused():
    with pytest.raises(AtlError, match="nothing to change"):
        client(FakeTransport()).confluence_edit("123")


def test_storage_text_goes_through_as_it_is():
    page = {"id": "123", "title": "Plan", "version": {"number": 1}, "body": {"storage": {"value": ""}}}
    transport = FakeTransport((200, json.dumps(page)), (200, json.dumps({"id": "123"})))
    client(transport).confluence_edit("123", "<table/>", storage=True)
    assert transport.calls[1][3]["body"] == {"representation": "storage", "value": "<table/>"}


def test_nothing_in_atl_can_delete():
    source = (Path(__file__).parent.parent / "src" / "atl_agent" / "client.py").read_text()
    assert '"DELETE"' not in source and "def confluence_delete" not in source


def test_cli_confluence_commands_use_the_confluence_token(capsys):
    seen = []

    def token(agent, product):
        seen.append(product)
        return TOKEN

    transport = FakeTransport(
        (200, json.dumps({"results": [{"id": "7"}]})),
        (200, json.dumps({"id": "200", "version": {"number": 1}, "_links": {"base": "https://x", "webui": "/p"}})),
    )
    args = ["haya", "confluence-create", "ELB", "text", "--title", "Design", "--cloud-id", CLOUD]
    assert main(args, token_source=token, transport=transport) == 0
    assert seen == ["confluence"]
    assert capsys.readouterr().out.strip() == "page 200 version 1 by Haya https://x/p"
    transport = FakeTransport((200, json.dumps({"results": []})))
    args = ["haya", "confluence-search", "plan", "--cloud-id", CLOUD]
    assert main(args, token_source=token, transport=transport) == 0
    assert seen == ["confluence", "confluence"] and json.loads(capsys.readouterr().out)["read_by"] == "Haya"


def test_cli_confluence_create_needs_a_title_and_text(capsys):
    assert main(["haya", "confluence-create", "ELB", "text", "--cloud-id", CLOUD], token_source=lambda a, p: TOKEN) == 2
    assert "--title" in capsys.readouterr().err


def test_via_sherif_uses_sherifs_jira_token_and_signs_as_maya(capsys):
    seen = []

    def token(agent, product):
        seen.append((agent, product))
        return TOKEN

    transport = FakeTransport((201, json.dumps({"id": "10", "author": {"displayName": "Sherif"}})))
    args = ["maya", "--via", "sherif", "jira-comment", "DFP-1", "Notes ready", "--cloud-id", CLOUD]
    assert main(args, token_source=token, transport=transport) == 0
    assert seen == [("sherif", "jira")]
    _, _, headers, body = transport.calls[0]
    assert base64.b64decode(headers["Authorization"].split()[1]).decode().startswith("sherif@")
    lines = [node.get("content", [{}])[0].get("text") for node in body["body"]["content"]]
    assert lines[-1] == "— Maya (Basita AI, via Sherif)"


def test_via_sherif_reads_jira_as_maya(capsys):
    seen = []

    def token(agent, product):
        seen.append(agent)
        return TOKEN

    transport = FakeTransport((200, json.dumps({"issues": []})))
    args = ["maya", "--via", "sherif", "jira-jql", "project = DFP", "--cloud-id", CLOUD]
    assert main(args, token_source=token, transport=transport) == 0
    assert seen == ["sherif"] and json.loads(capsys.readouterr().out)["read_by"] == "Maya (via Sherif)"


def test_maya_uses_her_own_user_on_confluence(capsys):
    seen = []

    def token(agent, product):
        seen.append((agent, product))
        return TOKEN

    page = {"id": "123", "title": "Plan", "version": {"number": 1}, "body": {"storage": {"value": ""}}, "results": []}
    logins = []

    def transport(method, url, headers, body):
        logins.append(base64.b64decode(headers["Authorization"].split()[1]).decode())
        return 200, json.dumps(page)

    assert main(["maya", "confluence-get", "123", "--cloud-id", CLOUD], token_source=token, transport=transport) == 0
    assert seen == [("maya", "confluence")]
    assert all(login.startswith("maya@") for login in logins) and logins


def test_yousef_uses_his_own_user_and_signature():
    c = client(FakeTransport(), "yousef")
    assert c.via == "" and c.signature == "Yousef (Basita AI)" and c.reader == "Yousef"


def test_an_agent_with_no_token_and_no_via_fails(capsys):
    def no_token(agent, product):
        raise AtlError(f"no Keychain item basita-atl-{agent}-{product}")

    assert main(["basel", "jira-get", "DFP-1", "--cloud-id", CLOUD], token_source=no_token) != 0
    assert "basita-atl-basel-jira" in capsys.readouterr().err


def test_without_via_an_agent_keeps_its_own_user_and_signature():
    c = client(FakeTransport(), "sherif")
    assert c.via == "" and c.signature == "Sherif (Basita AI)" and c.reader == "Sherif"


def test_via_refuses_an_account_that_does_not_exist():
    with pytest.raises(AtlError, match="no Atlassian user"):
        AtlClient("maya", CLOUD, TOKEN, via="mallory")


def test_a_page_edit_through_another_user_names_the_agent():
    page = {"id": "123", "title": "Plan", "version": {"number": 1}, "body": {"storage": {"value": ""}}}
    transport = FakeTransport((200, json.dumps(page)), (200, json.dumps({"id": "123"})))
    AtlClient("maya", CLOUD, TOKEN, transport=transport, via="sherif").confluence_edit("123", "new")
    assert transport.calls[1][3]["version"]["message"] == "[Maya (via Sherif)] new"


def _changelog(*changes):
    values = [{"items": [{"field": "assignee", "from": old, "to": new}]} for old, new in changes]
    return (200, json.dumps({"values": values, "isLast": True}))


FLOW_CASES = [
    ("malek", "submit", "In Development", "Ready for Testing", SALMA_ACCOUNT),
    ("haya", "submit", "In Development", "Ready for Testing", SALMA_ACCOUNT),
    # BASITA-147: the leads who take DFP work that isn't code hand it to Salma the same way.
    ("nora", "submit", "In Development", "Ready for Testing", SALMA_ACCOUNT),
    ("sherif", "submit", "In Development", "Ready for Testing", SALMA_ACCOUNT),
    ("salma", "pass", "In Testing", "Ready for Acceptance", ADAM_ACCOUNT),
    ("adam", "accept", "Ready for Acceptance", "Ready for Release", AHMED_ACCOUNT),
]


@pytest.mark.parametrize("agent, action, start, end, assignee", FLOW_CASES)
def test_flow_moves_and_assigns_the_next_person(agent, action, start, end, assignee):
    transport = FakeTransport(_status_issue(start, "indeterminate"), _to((end, "indeterminate")), (204, ""), (204, ""))
    assert client(transport, agent).jira_flow("DFP-1", action) == end
    assert transport.calls[2][0] == "PUT" and transport.calls[2][1].endswith("/assignee")
    assert transport.calls[2][3] == {"accountId": assignee}
    assert transport.calls[3][0] == "POST" and transport.calls[3][1].endswith("/transitions")


@pytest.mark.parametrize(
    "agent, action, start", [("salma", "fail", "In Testing"), ("adam", "reject", "Ready for Acceptance")]
)
def test_flow_goes_back_to_the_last_engineer(agent, action, start):
    transport = FakeTransport(
        _status_issue(start, "indeterminate"),
        _changelog(("x", MALEK_ID), (MALEK_ID, HAYA_ID), (HAYA_ID, SALMA_ACCOUNT), (SALMA_ACCOUNT, ADAM_ACCOUNT)),
        _to(("Ready for Development", "new")),
        (204, ""),
        (204, ""),
    )
    assert client(transport, agent).jira_flow("DFP-1", action) == "Ready for Development"
    assert transport.calls[3][3] == {"accountId": HAYA_ID}


def test_flow_finds_an_engineer_who_was_assigned_from_the_start():
    transport = FakeTransport(
        _status_issue("In Testing", "indeterminate"),
        _changelog((MALEK_ID, SALMA_ACCOUNT)),
        _to(("Ready for Development", "new")),
        (204, ""),
        (204, ""),
    )
    client(transport, "salma").jira_flow("DFP-1", "fail")
    assert transport.calls[3][3] == {"accountId": MALEK_ID}


def test_flow_reads_every_page_of_the_history():
    first = (200, json.dumps({"values": [{"items": [{"field": "assignee", "to": MALEK_ID}]}], "isLast": False}))
    second = (200, json.dumps({"values": [{"items": [{"field": "assignee", "to": HAYA_ID}]}], "isLast": True}))
    transport = FakeTransport(
        _status_issue("In Testing", "indeterminate"),
        first,
        second,
        _to(("Ready for Development", "new")),
        (204, ""),
        (204, ""),
    )
    client(transport, "salma").jira_flow("DFP-1", "fail")
    assert "startAt=1" in transport.calls[2][1] and transport.calls[4][3] == {"accountId": HAYA_ID}


def test_maya_submits_through_sherifs_user():
    transport = FakeTransport(
        _status_issue("In Development", "indeterminate"),
        _to(("Ready for Testing", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    maya = AtlClient("maya", CLOUD, TOKEN, transport=transport, via="sherif")
    assert maya.jira_flow("DFP-1", "submit") == "Ready for Testing"
    assert transport.calls[2][3] == {"accountId": SALMA_ACCOUNT}


@pytest.mark.parametrize("action, start", [("fail", "In Testing"), ("reject", "Ready for Acceptance")])
def test_flow_goes_back_to_the_lead_who_submitted_it(action, start):
    nora = JIRA_ACCOUNTS["nora"]
    transport = FakeTransport(
        _status_issue(start, "indeterminate"),
        _changelog(("x", MALEK_ID), (MALEK_ID, nora), (nora, SALMA_ACCOUNT), (SALMA_ACCOUNT, ADAM_ACCOUNT)),
        _to(("Ready for Development", "new")),
        (204, ""),
        (204, ""),
    )
    agent = "salma" if action == "fail" else "adam"
    client(transport, agent).jira_flow("DFP-1", action)
    assert transport.calls[3][3] == {"accountId": nora}


def test_flow_with_no_one_who_works_dfp_in_the_history_changes_nothing():
    transport = FakeTransport(_status_issue("In Testing", "indeterminate"), _changelog(("x", SALMA_ACCOUNT)))
    with pytest.raises(AtlError, match="no one who works DFP issues"):
        client(transport, "salma").jira_flow("DFP-1", "fail")
    assert all(call[0] == "GET" for call in transport.calls)


@pytest.mark.parametrize(
    "agent, action",
    [
        ("salma", "submit"),
        ("adam", "submit"),
        ("malek", "pass"),
        ("adam", "pass"),
        ("malek", "fail"),
        ("malek", "accept"),
        ("salma", "accept"),
        ("haya", "reject"),
        ("yousef", "submit"),
    ],
)
def test_flow_refuses_the_wrong_agent_before_any_call(agent, action):
    transport = FakeTransport()
    with pytest.raises(AtlError, match=f"{action} is for"):
        client(transport, agent).jira_flow("DFP-1", action)
    assert transport.calls == []


@pytest.mark.parametrize(
    "agent, action, status",
    [
        ("malek", "submit", "Ready for Development"),
        ("malek", "submit", "Ready for Testing"),
        ("salma", "pass", "Ready for Testing"),
        ("salma", "fail", "In Development"),
        ("adam", "accept", "In Testing"),
        ("adam", "reject", "Ready for Release"),
        ("adam", "accept", "Released"),
    ],
)
def test_flow_refuses_any_other_status_and_writes_nothing(agent, action, status):
    transport = FakeTransport(_status_issue(status, "indeterminate"))
    with pytest.raises(AtlError, match="only leaves"):
        client(transport, agent).jira_flow("DFP-1", action)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("key", ["BASITA-1", "ELB-1"])
def test_flow_is_for_dfp_only(key):
    transport = FakeTransport()
    with pytest.raises(AtlError, match="DFP only"):
        client(transport, "malek").jira_flow(key, "submit")
    assert transport.calls == []


def test_flow_refuses_an_unknown_action_such_as_a_status_name():
    transport = FakeTransport()
    with pytest.raises(AtlError, match="unknown action"):
        client(transport, "adam").jira_flow("DFP-1", "Released")
    assert transport.calls == []


def test_flow_with_no_transition_writes_nothing():
    transport = FakeTransport(_status_issue("In Development", "indeterminate"), _to(("Released", "done")))
    with pytest.raises(AtlError, match="no transition"):
        client(transport, "malek").jira_flow("DFP-1", "submit")
    assert all(call[0] == "GET" for call in transport.calls)


def test_flow_run_again_after_a_failed_move_finishes_the_step():
    failing = FakeTransport(
        _status_issue("In Development", "indeterminate"),
        _to(("Ready for Testing", "indeterminate")),
        (204, ""),
        (500, "{}"),
    )
    with pytest.raises(AtlError):
        client(failing, "malek").jira_flow("DFP-1", "submit")
    again = FakeTransport(
        _status_issue("In Development", "indeterminate"),
        _to(("Ready for Testing", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert client(again, "malek").jira_flow("DFP-1", "submit") == "Ready for Testing"


def test_cli_flow_comments_why_and_signs(capsys):
    transport = FakeTransport(
        _status_issue("In Testing", "indeterminate"),
        _to(("Ready for Acceptance", "indeterminate")),
        (204, ""),
        (204, ""),
        (201, json.dumps({"id": "5"})),
    )
    args = ["salma", "jira-flow", "DFP-1", "pass", "All criteria hold on the preview", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "DFP-1 -> Ready for Acceptance" in capsys.readouterr().out
    body = json.dumps(transport.calls[4][3])
    assert "All criteria hold on the preview" in body and "Ready for Acceptance" in body and "Salma (Basita AI)" in body


def test_cli_flow_needs_a_reason_and_moves_nothing(capsys):
    transport = FakeTransport()
    args = ["salma", "jira-flow", "DFP-1", "pass", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 2
    assert transport.calls == []


def test_only_jira_flow_takes_two_texts(capsys):
    transport = FakeTransport()
    args = ["salma", "jira-comment", "DFP-1", "one", "two", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 2
    assert transport.calls == []


def _created(key="DFP-9"):
    return (201, json.dumps({"id": "1", "key": key}))


def _create_body(transport):
    method, url, _, body = transport.calls[0]
    assert method == "POST" and url.endswith("/rest/api/3/issue")
    return body["fields"]


@pytest.mark.parametrize("project", ["BASITA", "DFP", "ELB"])
@pytest.mark.parametrize("issue_type", ["Story", "Task", "Bug"])
def test_create_returns_the_key_and_is_unassigned_except_a_dfp_bug(project, issue_type):
    transport = FakeTransport(_created(f"{project}-9"))
    assert client(transport, "malek").jira_create(project, issue_type, "A bug", "Steps") == f"{project}-9"
    fields = _create_body(transport)
    assert fields["project"] == {"key": project} and fields["issuetype"] == {"name": issue_type}
    expected = {"accountId": ADAM_ACCOUNT} if (project, issue_type) == ("DFP", "Bug") else None
    assert fields["summary"] == "A bug" and fields["assignee"] == expected
    assert "parent" not in fields and "labels" not in fields


def test_create_sets_the_parent_and_labels():
    transport = FakeTransport(_created())
    client(transport, "adam").jira_create("DFP", "Story", "S", "D", "DFP-1", ("a", "b"))
    fields = _create_body(transport)
    assert fields["parent"] == {"key": "DFP-1"} and fields["labels"] == ["a", "b"]


@pytest.mark.parametrize(
    "args, message",
    [
        (("OTHER", "Task", "S", "D"), "not a project"),
        (("DFP", "Epic", "S", "D"), "does not create an Epic"),
        (("DFP", "Sub-task", "S", "D"), "needs --parent"),
        (("DFP", "Subtask", "S", "D"), "needs --parent"),
        (("DFP", "task", "S", "D"), "does not create"),
        (("DFP", "Initiative", "S", "D"), "does not create"),
        (("DFP", "Task", "  ", "D"), "one line"),
        (("DFP", "Task", "two\nlines", "D"), "one line"),
        (("DFP", "Task", "x" * 256, "D"), "one line"),
        (("DFP", "Task", "S", "D", "ELB-1"), "not an issue in DFP"),
        (("DFP", "Task", "S", "D", "not a key"), "not an issue in DFP"),
        (("DFP", "Task", "S", "D", "", ("human-reviewed",)), "does not set the label"),
        (("DFP", "Task", "S", "D", "", ("reviewed-by-ahmed",)), "does not set the label"),
        (("DFP", "Task", "S", "D", "", ("needs-ahmed",)), "does not set the label"),
        (("DFP", "Task", "S", "D", "", ("two words",)), "does not set the label"),
    ],
)
def test_create_refuses_before_any_call(args, message):
    transport = FakeTransport()
    with pytest.raises(AtlError, match=message):
        client(transport, "salma").jira_create(*args)
    assert transport.calls == []


def test_cli_create_signs_the_description_and_prints_the_key(capsys):
    transport = FakeTransport(_created())
    args = ["salma", "jira-create", "DFP", "Bug", "Login fails", "Steps here", "--label", "ui", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "DFP-9 created, assigned to Adam, by Salma" in capsys.readouterr().out
    body = json.dumps(_create_body(transport)["description"], ensure_ascii=False)
    assert "Steps here" in body and "— Salma (Basita AI)" in body


def test_cli_create_via_uses_that_token_and_signs_as_the_agent(capsys):
    seen = []

    def token(agent, product):
        seen.append((agent, product))
        return TOKEN

    transport = FakeTransport(_created())
    args = ["maya", "--via", "sherif", "jira-create", "DFP", "Task", "S", "D", "--cloud-id", CLOUD]
    assert main(args, token_source=token, transport=transport) == 0
    assert seen == [("sherif", "jira")]
    assert base64.b64decode(transport.calls[0][2]["Authorization"].split()[1]).decode().startswith("sherif@")
    assert "— Maya (Basita AI, via Sherif)" in json.dumps(_create_body(transport)["description"], ensure_ascii=False)


@pytest.mark.parametrize(
    "extra", [[], ["DFP"], ["DFP", "Task"], ["DFP", "Task", "S"], ["DFP", "Task", "  ", "D"], ["DFP", "Task", "S", " "]]
)
def test_cli_create_needs_all_its_parts(extra, capsys):
    transport = FakeTransport()
    if not extra:
        with pytest.raises(SystemExit):
            main(["salma", "jira-create", "--cloud-id", CLOUD], token_source=lambda a, p: TOKEN, transport=transport)
    else:
        args = ["salma", "jira-create", *extra, "--cloud-id", CLOUD]
        assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 2
    assert transport.calls == []


def test_cli_create_reads_the_description_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("From stdin"))
    transport = FakeTransport(_created())
    args = ["salma", "jira-create", "DFP", "Bug", "S", "-", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "From stdin" in json.dumps(_create_body(transport)["description"])


def test_other_commands_refuse_a_fourth_text(capsys):
    transport = FakeTransport()
    args = ["salma", "jira-flow", "DFP-1", "pass", "why", "more", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 2
    assert transport.calls == []


@pytest.mark.parametrize("written", ["Sub-task", "Subtask"])
@pytest.mark.parametrize("agent", ["malek", "haya", "salma", "adam", "sherif", "nora", "yousef"])
def test_any_agent_creates_a_subtask_under_jiras_name(agent, written):
    transport = FakeTransport(_created())
    client(transport, agent).jira_create("DFP", written, "S", "D", "DFP-1")
    fields = _create_body(transport)
    assert fields["issuetype"] == {"name": "Subtask"} and fields["parent"] == {"key": "DFP-1"}


@pytest.mark.parametrize(
    "agent, project",
    [("nora", "BASITA"), ("nora", "DFP"), ("nora", "ELB"), ("sherif", "BASITA")],
)
def test_nora_and_sherif_create_epics(agent, project):
    transport = FakeTransport(_created(f"{project}-9"))
    client(transport, agent).jira_create(project, "Epic", "E", "D")
    assert _create_body(transport)["issuetype"] == {"name": "Epic"}


@pytest.mark.parametrize(
    "agent, project",
    [
        ("sherif", "DFP"),
        ("sherif", "ELB"),
        ("adam", "DFP"),
        ("malek", "BASITA"),
        ("salma", "DFP"),
        ("yousef", "BASITA"),
    ],
)
def test_others_do_not_create_epics(agent, project):
    transport = FakeTransport()
    with pytest.raises(AtlError, match="does not create an Epic"):
        client(transport, agent).jira_create(project, "Epic", "E", "D")
    assert transport.calls == []


def test_an_epic_has_no_parent():
    transport = FakeTransport()
    with pytest.raises(AtlError, match="no parent"):
        client(transport, "nora").jira_create("DFP", "Epic", "E", "D", "DFP-1")
    assert transport.calls == []


@pytest.mark.parametrize("agent", ["adam", "nora", "sherif", "salma"])
def test_four_agents_assign_on_create(agent):
    transport = FakeTransport(_created())
    client(transport, agent).jira_create("DFP", "Task", "S", "D", assignee="Malek")
    assert _create_body(transport)["assignee"] == {"accountId": MALEK_ID}


@pytest.mark.parametrize("agent", ["malek", "haya", "yousef", "maya"])
def test_others_do_not_assign_on_create(agent):
    transport = FakeTransport()
    with pytest.raises(AtlError, match="does not assign"):
        client(transport, agent).jira_create("DFP", "Task", "S", "D", assignee="salma")
    assert transport.calls == []


def test_maya_through_sherif_is_still_maya_and_does_not_assign():
    transport = FakeTransport()
    with pytest.raises(AtlError, match="Maya does not assign"):
        AtlClient("maya", CLOUD, TOKEN, transport=transport, via="sherif").jira_create(
            "DFP", "Task", "S", "D", assignee="salma"
        )
    assert transport.calls == []


@pytest.mark.parametrize("name", ["maya", "basel", "talia", "mallory", "712020:someone"])
def test_assign_refuses_a_name_with_no_seat(name):
    transport = FakeTransport()
    with pytest.raises(AtlError, match="no Jira seat"):
        client(transport, "sherif").jira_create("DFP", "Task", "S", "D", assignee=name)
    assert transport.calls == []


def test_cli_create_assigns_and_says_so(capsys):
    transport = FakeTransport(_created())
    args = ["sherif", "jira-create", "ELB", "Task", "S", "D", "--assignee", "malek", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "assigned to Malek" in capsys.readouterr().out


def test_cli_create_links_with_relates_to(capsys):
    transport = FakeTransport(_created(), (201, ""))
    args = ["salma", "jira-create", "DFP", "Bug", "S", "D", "--link", "DFP-2", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    method, url, _, body = transport.calls[1]
    assert method == "POST" and url.endswith("/rest/api/3/issueLink")
    assert body == {"type": {"name": "Relates"}, "outwardIssue": {"key": "DFP-9"}, "inwardIssue": {"key": "DFP-2"}}
    assert "DFP-9 relates to DFP-2" in capsys.readouterr().out


def test_cli_create_refuses_a_bad_link_before_creating(capsys):
    transport = FakeTransport()
    args = ["salma", "jira-create", "DFP", "Bug", "S", "D", "--link", "nope", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 1
    assert transport.calls == []


def test_cli_create_names_the_issue_when_the_link_fails(capsys):
    transport = FakeTransport(_created(), (404, "{}"))
    args = ["salma", "jira-create", "DFP", "Bug", "S", "D", "--link", "DFP-404", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 1
    captured = capsys.readouterr()
    assert "DFP-9 created" in captured.out and "DFP-9 was created, but not linked to DFP-404" in captured.err


@pytest.mark.parametrize("agent", ["salma", "malek", "haya", "adam", "sherif", "nora", "yousef"])
@pytest.mark.parametrize("assignee", ["", "adam", "Adam"])
def test_a_dfp_bug_goes_to_adam_from_any_agent(agent, assignee):
    transport = FakeTransport(_created())
    client(transport, agent).jira_create("DFP", "Bug", "S", "D", assignee=assignee)
    assert _create_body(transport)["assignee"] == {"accountId": ADAM_ACCOUNT}


@pytest.mark.parametrize(
    "agent, assignee",
    [
        ("salma", "ahmed"),
        ("salma", "salma"),
        ("salma", "maya"),
        ("malek", "malek"),
        ("malek", "haya"),
        ("sherif", "malek"),
        ("adam", "haya"),
        ("nora", "malek"),
    ],
)
def test_a_dfp_bug_refuses_any_other_assignee(agent, assignee):
    transport = FakeTransport()
    with pytest.raises(AtlError, match="goes to Adam"):
        client(transport, agent).jira_create("DFP", "Bug", "S", "D", assignee=assignee, link="DFP-1")
    assert transport.calls == []


def _versions(*versions):
    return (200, json.dumps({"fields": {"fixVersions": list(versions)}}))


@pytest.mark.parametrize("engineer, account", [("malek", MALEK_ID), ("Haya", HAYA_ID)])
def test_a_technical_bug_from_salma_goes_to_the_engineer_ready_to_start(engineer, account):
    transport = FakeTransport(
        _versions({"id": "1", "released": True}, {"id": "2", "released": False}),
        _created(),
        _to(("In Development", "indeterminate"), ("Ready for Development", "new")),
        (204, ""),
    )
    key = client(transport, "salma").jira_create("DFP", "Bug", "S", "D", assignee=engineer, link="DFP-1")
    assert key == "DFP-9"
    assert transport.calls[0][1].endswith("/issue/DFP-1?fields=fixVersions")
    fields = transport.calls[1][3]["fields"]
    assert fields["assignee"] == {"accountId": account} and fields["fixVersions"] == [{"id": "2"}]
    assert transport.calls[3][1].endswith("/DFP-9/transitions")


@pytest.mark.parametrize("link", ["", "nope", "ELB-1"])
def test_a_technical_bug_needs_a_dfp_link(link):
    transport = FakeTransport()
    with pytest.raises(AtlError, match="needs --link"):
        client(transport, "salma").jira_create("DFP", "Bug", "S", "D", assignee="malek", link=link)
    assert transport.calls == []


@pytest.mark.parametrize("versions", [[], [{"id": "1", "released": True}]])
def test_a_technical_bug_needs_an_unreleased_version_and_creates_nothing(versions):
    transport = FakeTransport(_versions(*versions))
    with pytest.raises(AtlError, match="no unreleased Fix Version"):
        client(transport, "salma").jira_create("DFP", "Bug", "S", "D", assignee="malek", link="DFP-1")
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "after",
    [[_to(("Closed", "done"))], [_to(("Ready for Development", "new")), (500, "{}")], [(500, "{}")]],
)
def test_a_technical_bug_names_the_new_key_when_the_move_fails(after):
    transport = FakeTransport(_versions({"id": "2", "released": False}), _created(), *after)
    with pytest.raises(AtlError, match="DFP-9 was created and assigned to Malek, but not moved"):
        client(transport, "salma").jira_create("DFP", "Bug", "S", "D", assignee="malek", link="DFP-1")


def test_cli_technical_bug_moves_links_and_says_so(capsys):
    transport = FakeTransport(
        _versions({"id": "2", "released": False}),
        _created(),
        _to(("Ready for Development", "new")),
        (204, ""),
        (201, ""),
    )
    args = [
        "salma",
        "jira-create",
        "DFP",
        "Bug",
        "S",
        "D",
        "--assignee",
        "malek",
        "--link",
        "DFP-1",
        "--cloud-id",
        CLOUD,
    ]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    out = capsys.readouterr().out
    assert "DFP-9 created, assigned to Malek, by Salma" in out and "DFP-9 relates to DFP-1" in out


def test_an_ordinary_dfp_bug_is_not_moved_and_takes_no_version():
    transport = FakeTransport(_created())
    client(transport, "salma").jira_create("DFP", "Bug", "S", "D", link="DFP-1")
    assert len(transport.calls) == 1 and "fixVersions" not in _create_body(transport)


def test_a_dfp_bug_from_maya_through_sherif_goes_to_adam(capsys):
    seen = []

    def token(agent, product):
        seen.append(agent)
        return TOKEN

    transport = FakeTransport(_created())
    args = ["maya", "--via", "sherif", "jira-create", "DFP", "Bug", "S", "D", "--cloud-id", CLOUD]
    assert main(args, token_source=token, transport=transport) == 0
    assert seen == ["sherif"] and _create_body(transport)["assignee"] == {"accountId": ADAM_ACCOUNT}
    assert "assigned to Adam, by Maya (via Sherif)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "agent, project, issue_type, parent",
    [
        ("salma", "BASITA", "Bug", ""),
        ("salma", "ELB", "Bug", ""),
        ("malek", "ELB", "Bug", ""),
        ("salma", "DFP", "Task", ""),
        ("salma", "DFP", "Story", ""),
        ("salma", "DFP", "Sub-task", "DFP-1"),
        ("sherif", "DFP", "Task", ""),
    ],
)
def test_everything_else_stays_unassigned(agent, project, issue_type, parent):
    transport = FakeTransport(_created(f"{project}-9"))
    client(transport, agent).jira_create(project, issue_type, "S", "D", parent)
    assert _create_body(transport)["assignee"] is None


@pytest.mark.parametrize("project, issue_type", [("ELB", "Bug"), ("BASITA", "Bug"), ("DFP", "Task")])
def test_only_the_four_still_assign_elsewhere(project, issue_type):
    transport = FakeTransport(_created(f"{project}-9"))
    client(transport, "salma").jira_create(project, issue_type, "S", "D", assignee="malek")
    assert _create_body(transport)["assignee"] == {"accountId": MALEK_ID}
    with pytest.raises(AtlError, match="does not assign"):
        client(FakeTransport(), "malek").jira_create(project, issue_type, "S", "D", assignee="salma")


def test_cli_says_a_dfp_bug_is_assigned_to_adam(capsys):
    transport = FakeTransport(_created())
    args = ["salma", "jira-create", "DFP", "Bug", "S", "D", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    assert "DFP-9 created, assigned to Adam, by Salma" in capsys.readouterr().out


def _nodes(text):
    return _adf(text)["content"][0]["content"]


def test_at_name_becomes_a_real_mention_in_place():
    nodes = _nodes("Please check @Malek now")
    assert nodes[0] == {"type": "text", "text": "Please check "}
    assert nodes[1] == {"type": "mention", "attrs": {"id": MALEK_ID, "text": "@Malek"}}
    assert nodes[2] == {"type": "text", "text": " now"}


@pytest.mark.parametrize("prefix", ["FYI ", "CC ", "cc: "])
def test_fyi_and_cc_keep_their_word_and_mention_too(prefix):
    nodes = _nodes(f"{prefix}@haya")
    assert nodes[0]["text"] == prefix and nodes[1]["attrs"] == {"id": HAYA_ID, "text": "@Haya"}


def test_several_mentions_on_a_line():
    nodes = _nodes("CC @Malek CC @Haya, @Ahmed")
    assert [n["attrs"]["text"] for n in nodes if n["type"] == "mention"] == ["@Malek", "@Haya", "@Ahmed"]


@pytest.mark.parametrize(
    "text",
    [
        "Malek should look",
        "mail a@malek.com",
        "@Talia please",
        "@Maya please",
        "@Basel",
        "@Malekah",
        "x/@malek",
        "@@malek",
    ],
)
def test_no_mention_for_a_plain_name_an_address_or_no_seat(text):
    assert all(node["type"] == "text" for node in _nodes(text))


def test_links_and_mentions_on_one_line():
    nodes = _nodes("@Malek see https://example.com/x and @Haya")
    kinds = [n["type"] for n in nodes]
    assert kinds == ["mention", "text", "text", "text", "mention"]
    assert nodes[2]["marks"][0]["attrs"]["href"] == "https://example.com/x"


def test_cli_comment_mentions_and_reads_back(capsys):
    transport = FakeTransport((201, json.dumps({"id": "5", "author": {"displayName": "Salma"}})))
    args = ["salma", "jira-comment", "DFP-1", "@Malek please look", "--cloud-id", CLOUD]
    assert main(args, token_source=lambda a, p: TOKEN, transport=transport) == 0
    body = transport.calls[0][3]["body"]
    assert body["content"][0]["content"][0]["attrs"]["id"] == MALEK_ID
    assert _adf_text(body["content"][0]).startswith("@Malek please look")
