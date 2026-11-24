"""The Fix Version rule (Ahmed, 24/11/2026, decision 21): jira-start refuses an in-scope issue with no unreleased
Fix Version; jira-ready only warns; one shared scope list."""

import json

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main
from atl_agent.fixversion import has_unreleased, in_scope, load_scope

TOKEN = "tok-" + "x" * 12


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split("/jira/cloud-1")[-1], json.loads(body) if body else None))
        return self.responses.pop(0)


def client(transport, agent="malek"):
    return AtlClient(agent, "cloud-1", TOKEN, transport=transport)


def issue(kind="Story", versions=(), labels=(), status="Ready for Development", category="new", parent=None):
    fields = {
        "status": {"name": status, "statusCategory": {"key": category}},
        "labels": list(labels),
        "issuetype": {"name": kind},
        "fixVersions": list(versions),
    }
    if parent:
        fields["parent"] = {"key": parent}
    return (200, json.dumps({"fields": fields}))


OPEN = {"id": "7", "name": "0.3.2", "released": False}
DONE = {"id": "6", "name": "0.3.1", "released": True}
MOVES = (
    200,
    json.dumps(
        {"transitions": [{"id": "21", "to": {"name": "In Development", "statusCategory": {"key": "indeterminate"}}}]}
    ),
)


@pytest.mark.parametrize("versions", [(), (DONE,)])
@pytest.mark.parametrize("kind", ["Story", "Task", "Bug"])
def test_start_refuses_an_in_scope_issue_without_an_unreleased_version(kind, versions):
    transport = FakeTransport(issue(kind, versions))
    with pytest.raises(AtlError, match="no unreleased Fix Version"):
        client(transport).jira_start("DFP-9")
    assert len(transport.calls) == 1  # nothing written, no transition read


def test_start_with_an_unreleased_version_goes_on():
    transport = FakeTransport(issue("Story", (DONE, OPEN)), MOVES, (204, ""), (204, ""))
    assert client(transport).jira_start("DFP-9") == "In Development"


@pytest.mark.parametrize(
    ("key", "kind", "labels"),
    [
        ("DFP-9", "Epic", ()),
        ("ELB-9", "Story", ("milestone",)),
        ("ELB-9", "Task", ("project-plan",)),
        ("ELB-9", "Task", ("ahmed-decision", "x")),
        ("BASITA-9", "Task", ()),
    ],
)
def test_start_leaves_out_of_scope_issues_alone(key, kind, labels):
    transport = FakeTransport(issue(kind, (), labels), MOVES, (204, ""), (204, ""))
    assert client(transport).jira_start(key) == "In Development"


def test_a_subtask_uses_its_parents_version():
    ok = FakeTransport(
        issue("Subtask", (), parent="DFP-1"),
        (200, json.dumps({"fields": {"fixVersions": [OPEN]}})),
        MOVES,
        (204, ""),
        (204, ""),
    )
    assert client(ok).jira_start("DFP-2") == "In Development"
    assert ok.calls[1][1] == "/rest/api/3/issue/DFP-1?fields=fixVersions"
    # its own version does not count, the parent's does
    bad = FakeTransport(
        issue("Subtask", (OPEN,), parent="DFP-1"), (200, json.dumps({"fields": {"fixVersions": [DONE]}}))
    )
    with pytest.raises(AtlError, match="its parent DFP-1 has no unreleased Fix Version"):
        client(bad).jira_start("DFP-2")


def refining(versions):
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "AC1 It works."}]}]}
    fields = {
        "status": {"name": "Refinement"},
        "issuetype": {"name": "Story"},
        "fixVersions": list(versions),
        "description": doc,
    }
    return (200, json.dumps({"fields": fields}))


def test_ready_warns_without_a_version_and_still_moves():
    transport = FakeTransport(
        refining(()),
        (200, json.dumps({"transitions": [{"id": "3", "to": {"name": "Ready for Development"}}]})),
        (204, ""),
        (204, ""),
    )
    c = client(transport, "adam")
    c._assignee_account = lambda name: "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"
    assert c.jira_ready("DFP-88", "malek") == "Ready for Development"
    assert len(c.warnings) == 1 and "required before In Development" in c.warnings[0]


def test_ready_with_a_version_does_not_warn():
    transport = FakeTransport(
        refining((OPEN,)),
        (200, json.dumps({"transitions": [{"id": "3", "to": {"name": "Ready for Development"}}]})),
        (204, ""),
        (204, ""),
    )
    c = client(transport, "adam")
    c._assignee_account = lambda name: "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"
    c.jira_ready("DFP-88", "malek")
    assert c.warnings == []


def test_cli_ready_prints_the_warning_and_puts_it_in_the_comment(capsys):
    transport = FakeTransport(
        refining(()),
        (200, json.dumps({"transitions": [{"id": "3", "to": {"name": "Ready for Development"}}]})),
        (204, ""),
        (204, ""),
        (201, "{}"),
    )
    argv = ["adam", "jira-ready", "DFP-88", "ready", "--assignee", "malek", "--cloud-id", "cloud-1"]
    import atl_agent.client as cl

    original = cl.AtlClient._assignee_account
    cl.AtlClient._assignee_account = lambda self, name: "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"
    try:
        assert main(argv, token_source=lambda a, p: TOKEN, transport=transport) == 0
    finally:
        cl.AtlClient._assignee_account = original
    assert "warning" in capsys.readouterr().err
    assert "Heads-up" in json.dumps(transport.calls[-1][2])


def test_the_scope_file_is_valid_and_matches_the_rule():
    scope = load_scope()
    assert scope["projects"] == ["DFP", "ELB"]
    assert scope["issue_types"] == ["Story", "Task", "Bug", "Subtask"]
    assert set(scope["exclude_labels"]) == {"milestone", "project-plan", "ahmed-decision"}
    assert "In Development" in scope["guarded_statuses"] and "Ready for Development" not in scope["guarded_statuses"]
    assert in_scope("DFP-1", "Story", [], scope) and not in_scope("DFP-1", "Epic", [], scope)
    assert has_unreleased([DONE, OPEN]) and not has_unreleased([DONE]) and not has_unreleased(None)
