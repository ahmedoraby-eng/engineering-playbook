import io
import json

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main as cli
from atl_agent.close import close_issues, closing_keys, main

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split("/cloud-1")[1], json.loads(body) if body else None))
        return self.responses.pop(0)


def status(name, category):
    return (200, json.dumps({"fields": {"status": {"name": name, "statusCategory": {"key": category}}}}))


def transitions(*pairs):
    return (
        200,
        json.dumps(
            {
                "transitions": [
                    {"id": tid, "to": {"name": name, "statusCategory": {"key": cat}}} for tid, name, cat in pairs
                ]
            }
        ),
    )


def client(transport):
    return AtlClient("talia", "cloud-1", TOKEN, transport=transport)


def test_only_closes_counts_and_comments_are_ignored():
    body = """Closes BASITA-1
    Refs DFP-12
    <!-- Closes ELB-9 -->
    closes: dfp-3 and Closes BASITA-1 again, Closes OTHER-5, Closes <KEY>, Disclosures BASITA-8
    """
    assert closing_keys(body) == ["BASITA-1", "DFP-3"]
    assert closing_keys("") == [] and closing_keys(None) == []


def test_close_moves_to_done_and_comments_with_links():
    transport = FakeTransport(
        status("To Do", "new"),
        transitions(("11", "In Progress", "indeterminate"), ("31", "Done", "done")),
        (204, ""),
        (201, json.dumps({"id": "5"})),
    )
    lines = close_issues(client(transport), ["BASITA-1"], "https://github.com/o/r/pull/7", "- o/r#7 merged")
    assert lines == ["BASITA-1: To Do -> Done"]
    assert transport.calls[2] == ("POST", "/rest/api/3/issue/BASITA-1/transitions", {"transition": {"id": "31"}})
    comment = transport.calls[3][2]["body"]["content"]
    assert "https://github.com/o/r/pull/7 was merged" in comment[0]["content"][0]["text"]
    assert any("o/r#7 merged" in p.get("content", [{}])[0].get("text", "") for p in comment)


def test_already_done_is_left_alone():
    transport = FakeTransport(status("Done", "done"))
    assert close_issues(client(transport), ["DFP-2"], "u") == ["DFP-2: already Done, nothing to do"]
    assert len(transport.calls) == 1


def test_no_done_transition_is_an_error(capsys):
    transport = FakeTransport(status("To Do", "new"), transitions(("11", "In Progress", "indeterminate")))
    code = main(
        ["--pr-url", "u"],
        stdin=io.StringIO("Closes BASITA-1"),
        environ={"ATL_CLOUD_ID": "cloud-1", "ATL_TOKEN": TOKEN},
        transport=transport,
    )
    assert code == 1
    assert "no transition to a Done status" in capsys.readouterr().err


def test_main_without_keys_or_dry_run_touches_nothing(capsys):
    assert main(["--pr-url", "u"], stdin=io.StringIO("Refs BASITA-1")) == 0
    assert "nothing to close" in capsys.readouterr().out
    assert main(["--pr-url", "u", "--dry-run"], stdin=io.StringIO("Closes DFP-4")) == 0
    assert "Would close: DFP-4" in capsys.readouterr().out


def test_main_closes_and_reads_the_links_file(tmp_path, capsys):
    links = tmp_path / "links.txt"
    links.write_text("- a/b#1 merged\n")
    transport = FakeTransport(
        status("In Progress", "indeterminate"), transitions(("31", "Done", "done")), (204, ""), (201, "{}")
    )
    code = main(
        ["--pr-url", "u", "--links-file", str(links)],
        stdin=io.StringIO("Closes ELB-7"),
        environ={"ATL_CLOUD_ID": "cloud-1", "ATL_TOKEN": TOKEN},
        transport=transport,
    )
    assert code == 0 and "ELB-7: In Progress -> Done" in capsys.readouterr().out


def issue(status_name, category, labels=()):
    fields = {"status": {"name": status_name, "statusCategory": {"key": category}}, "labels": list(labels)}
    return (200, json.dumps({"fields": fields}))


def test_start_moves_to_do_to_in_progress_and_labels_it():
    transport = FakeTransport(
        issue("To Do", "new"),
        transitions(("21", "In Progress", "indeterminate"), ("31", "Done", "done")),
        (204, ""),
        (204, ""),
    )
    assert client(transport).jira_start("DFP-12") == "In Progress"
    assert transport.calls[2] == ("PUT", "/rest/api/3/issue/DFP-12", {"update": {"labels": [{"add": "agent-picked"}]}})
    assert transport.calls[3] == ("POST", "/rest/api/3/issue/DFP-12/transitions", {"transition": {"id": "21"}})


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (issue("To Do", "new", ["agent-picked"]), "already has the 'agent-picked' label"),
        (issue("In Review", "indeterminate"), "only starts issues that are ready"),
    ],
)
def test_start_refuses_a_second_pickup_or_a_started_issue(response, message):
    transport = FakeTransport(response)
    with pytest.raises(AtlError, match=message):
        client(transport).jira_start("DFP-12")
    assert len(transport.calls) == 1  # nothing written


def test_start_without_in_progress_transition_writes_nothing():
    transport = FakeTransport(issue("To Do", "new"), transitions(("31", "Done", "done")))
    with pytest.raises(AtlError, match="no transition to In Development or In Progress"):
        client(transport).jira_start("DFP-12")
    assert [c[0] for c in transport.calls] == ["GET", "GET"]


def test_comment_mentions_people_first():
    transport = FakeTransport((201, "{}"))
    client(transport).jira_comment("ELB-3", "hello", ("acc-1",))
    first = transport.calls[0][2]["body"]["content"][0]["content"]
    assert first[0] == {"type": "mention", "attrs": {"id": "acc-1"}}


def test_cli_signs_comments_and_starts_issues(capsys):
    def run(*argv, responses):
        transport = FakeTransport(*responses)
        code = cli(["malek", *argv, "--cloud-id", "cloud-1"], token_source=lambda a, p: TOKEN, transport=transport)
        return code, transport

    code, transport = run("jira-comment", "ELB-3", "reply", "--mention", "acc-9", responses=[(201, "{}")])
    body = transport.calls[0][2]["body"]["content"]
    assert code == 0 and body[0]["content"][0]["attrs"]["id"] == "acc-9"
    assert body[-1]["content"][0]["text"] == "— Malek (Basita AI)"

    code, transport = run(
        "jira-start",
        "DFP-12",
        responses=[
            issue("To Do", "new"),
            transitions(("21", "In Progress", "indeterminate")),
            (204, ""),
            (204, ""),
            (201, "{}"),
        ],
    )
    assert code == 0 and "DFP-12 -> In Progress" in capsys.readouterr().out
    assert transport.calls[-1][2]["body"]["content"][0]["content"][0]["text"] == "Picked up; working on it now."


def test_start_from_ready_for_development_goes_to_in_development():
    transport = FakeTransport(
        issue("Ready for Development", "new"),
        transitions(("40", "In Testing", "indeterminate"), ("41", "In Development", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert client(transport).jira_start("DFP-12") == "In Development"
    assert transport.calls[3][2] == {"transition": {"id": "41"}}


def test_start_refuses_refinement():
    transport = FakeTransport(issue("Refinement", "new"))
    with pytest.raises(AtlError, match="refine it first"):
        client(transport).jira_start("DFP-88")


def test_refinement_job_labels_then_moves_to_ready_for_development():
    transport = FakeTransport(issue("Refinement", "new"), (204, ""))
    client(transport).jira_refine_start("DFP-88")
    assert transport.calls[1][2] == {"update": {"labels": [{"add": "agent-refining"}]}}

    transport = FakeTransport(
        issue("Refinement", "new", ["agent-refining"]),
        transitions(("21", "In Development", "indeterminate"), ("14", "Ready for Development", "new")),
        (204, ""),
        (204, ""),
    )
    assert client(transport).jira_refined("DFP-88") == "Ready for Development"
    assert transport.calls[2][2] == {"transition": {"id": "14"}}
    assert transport.calls[3][2] == {"update": {"labels": [{"remove": "agent-refining"}]}}


@pytest.mark.parametrize(
    ("method", "response", "message"),
    [
        ("jira_refine_start", issue("To Do", "new"), "not Refinement"),
        ("jira_refine_start", issue("Refinement", "new", ["agent-refining"]), "not refining it again"),
        ("jira_refined", issue("In Development", "indeterminate"), "not Refinement"),
    ],
)
def test_refinement_guards(method, response, message):
    with pytest.raises(AtlError, match=message):
        getattr(client(FakeTransport(response)), method)("DFP-88")


def test_refined_without_ready_transition_is_an_error():
    transport = FakeTransport(issue("Refinement", "new"), transitions(("21", "In Development", "indeterminate")))
    with pytest.raises(AtlError, match="no transition to Ready for Development"):
        client(transport).jira_refined("DFP-88")


def test_review_moves_to_ready_for_acceptance_labels_and_assigns():
    transport = FakeTransport(
        issue("In Development", "indeterminate"),
        transitions(("50", "Ready for Acceptance", "indeterminate"), ("60", "Released", "done")),
        (204, ""),
        (204, ""),
        (204, ""),
    )
    assert client(transport).jira_review("DFP-12", "acc-ahmed") == "Ready for Acceptance"
    assert transport.calls[2][2] == {"transition": {"id": "50"}}
    assert transport.calls[3][2] == {"update": {"labels": [{"add": "awaiting-approval"}]}}
    assert transport.calls[4] == ("PUT", "/rest/api/3/issue/DFP-12/assignee", {"accountId": "acc-ahmed"})


def test_review_without_review_status_only_labels_and_assigns():
    transport = FakeTransport(
        issue("In Progress", "indeterminate"), transitions(("31", "Done", "done")), (204, ""), (204, "")
    )
    assert client(transport).jira_review("BASITA-9", "acc-yamen") == "In Progress"
    assert [c[0] for c in transport.calls] == ["GET", "GET", "PUT", "PUT"]


@pytest.mark.parametrize(
    ("approver", "response", "message"),
    [("", None, "approver account id is needed"), ("acc", issue("Done", "done"), "already Done")],
)
def test_review_guards(approver, response, message):
    transport = FakeTransport(*([response] if response else []))
    with pytest.raises(AtlError, match=message):
        client(transport).jira_review("BASITA-9", approver)


def test_link_blocker_is_the_inward_issue():
    transport = FakeTransport((201, ""))
    client(transport).jira_link("DFP-3", "DFP-12")
    assert transport.calls[0] == (
        "POST",
        "/rest/api/3/issueLink",
        {"type": {"name": "Blocks"}, "inwardIssue": {"key": "DFP-3"}, "outwardIssue": {"key": "DFP-12"}},
    )


def test_close_leaves_an_issue_awaiting_approval_open():
    transport = FakeTransport(
        (
            200,
            json.dumps(
                {
                    "fields": {
                        "status": {"name": "Ready for Acceptance", "statusCategory": {"key": "indeterminate"}},
                        "labels": ["awaiting-approval"],
                    }
                }
            ),
        ),
        (201, "{}"),
    )
    assert close_issues(client(transport), ["DFP-12"], "u") == ["DFP-12: awaiting approval, left open"]
    assert [c[0] for c in transport.calls] == ["GET", "POST"]


def test_cli_refinement_review_and_link(capsys):
    def run(*argv, responses):
        transport = FakeTransport(*responses)
        code = cli(["haya", *argv, "--cloud-id", "cloud-1"], token_source=lambda a, p: TOKEN, transport=transport)
        return code, transport

    code, _ = run("jira-refine-start", "DFP-88", responses=[issue("Refinement", "new"), (204, ""), (201, "{}")])
    assert code == 0
    code, _ = run(
        "jira-refined",
        "DFP-88",
        responses=[
            issue("Refinement", "new"),
            transitions(("14", "Ready for Development", "new")),
            (204, ""),
            (204, ""),
            (201, "{}"),
        ],
    )
    assert code == 0
    code, transport = run(
        "jira-review",
        "BASITA-9",
        "--to",
        "acc-ahmed",
        responses=[issue("In Progress", "indeterminate"), transitions(), (204, ""), (204, ""), (201, "{}")],
    )
    assert code == 0 and transport.calls[-1][2]["body"]["content"][0]["content"][0]["attrs"]["id"] == "acc-ahmed"
    code, transport = run("jira-link", "DFP-3", "DFP-12", responses=[(201, "")])
    assert code == 0 and transport.calls[0][1] == "/rest/api/3/issueLink"
    out = capsys.readouterr().out
    assert (
        "refining" in out and "Ready for Development" in out and "awaiting approval" in out and "blocks DFP-12" in out
    )


def test_start_prefers_in_development_when_both_exist():
    transport = FakeTransport(
        issue("To Do", "new"),
        transitions(("21", "In Progress", "indeterminate"), ("41", "In Development", "indeterminate")),
        (204, ""),
        (204, ""),
    )
    assert client(transport).jira_start("ELB-3") == "In Development"
