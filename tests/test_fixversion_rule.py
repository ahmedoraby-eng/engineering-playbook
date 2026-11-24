"""The Fix Version rule (Ahmed, 24/11/2026, decision 21): jira-start refuses an in-scope issue with no unreleased
Fix Version; jira-ready only warns; one shared scope list."""

import json

import pytest

from atl_agent import AtlClient, AtlError
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


def refining(versions, kind="Story", labels=(), parent=None):
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "AC1 It works."}]}]}
    fields = {
        "status": {"name": "Refinement"},
        "issuetype": {"name": kind},
        "fixVersions": list(versions),
        "description": doc,
        "labels": list(labels),
    }
    if parent:
        fields["parent"] = {"key": parent}
    return (200, json.dumps({"fields": fields}))


@pytest.mark.parametrize("versions", [(), (DONE,)])
def test_ready_refuses_without_an_unreleased_version_and_writes_nothing(versions):
    transport = FakeTransport(refining(versions))
    with pytest.raises(AtlError) as error:
        client(transport, "adam").jira_ready("DFP-88", "malek")
    assert "no unreleased Fix Version" in str(error.value) and "assign it to Adam again" in str(error.value)
    assert len(transport.calls) == 1


def test_ready_with_a_version_moves():
    transport = FakeTransport(
        refining((OPEN,)),
        (200, json.dumps({"transitions": [{"id": "3", "to": {"name": "Ready for Development"}}]})),
        (204, ""),
        (204, ""),
    )
    c = client(transport, "adam")
    c._assignee_account = lambda name: "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"
    assert c.jira_ready("DFP-88", "malek") == "Ready for Development"


def test_ready_leaves_out_of_scope_issues_alone():
    transport = FakeTransport(
        refining((), kind="Story", labels=["milestone"]),
        (200, json.dumps({"transitions": [{"id": "3", "to": {"name": "Ready for Development"}}]})),
        (204, ""),
        (204, ""),
    )
    c = client(transport, "adam")
    c._assignee_account = lambda name: "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"
    assert c.jira_ready("DFP-88", "malek") == "Ready for Development"


def test_ready_for_a_subtask_uses_its_parents_version():
    bad = FakeTransport(
        refining((OPEN,), kind="Subtask", parent="DFP-1"), (200, json.dumps({"fields": {"fixVersions": [DONE]}}))
    )
    with pytest.raises(AtlError, match="its parent DFP-1 has no unreleased Fix Version"):
        client(bad, "adam").jira_ready("DFP-88", "malek")
    ok = FakeTransport(
        refining((), kind="Subtask", parent="DFP-1"),
        (200, json.dumps({"fields": {"fixVersions": [OPEN]}})),
        (200, json.dumps({"transitions": [{"id": "3", "to": {"name": "Ready for Development"}}]})),
        (204, ""),
        (204, ""),
    )
    c = client(ok, "adam")
    c._assignee_account = lambda name: "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"
    assert c.jira_ready("DFP-88", "malek") == "Ready for Development"


def test_the_scope_file_is_valid_and_matches_the_rule():
    scope = load_scope()
    assert scope["projects"] == ["DFP", "ELB"]
    assert scope["issue_types"] == ["Story", "Task", "Bug", "Subtask"]
    assert set(scope["exclude_labels"]) == {"milestone", "project-plan", "ahmed-decision"}
    assert "In Development" in scope["guarded_statuses"] and "Ready for Development" in scope["ready_statuses"]
    assert in_scope("DFP-1", "Story", [], scope) and not in_scope("DFP-1", "Epic", [], scope)
    assert has_unreleased([DONE, OPEN]) and not has_unreleased([DONE]) and not has_unreleased(None)


def test_unready_sends_a_ready_issue_back_to_refinement_for_adam():
    transport = FakeTransport(
        (200, json.dumps({"fields": {"status": {"name": "Ready for Development", "statusCategory": {"key": "new"}}}})),
        (200, json.dumps({"transitions": [{"id": "9", "to": {"name": "Refinement"}}]})),
        (204, ""),
        (204, ""),
    )
    assert client(transport, "adam").jira_flow("DFP-5", "unready") == "Refinement"
    assert transport.calls[-2][2] == {"accountId": "712020:74556401-e243-4c53-9f59-75f28fdf6cec"}


def test_unready_is_adams_and_leaves_only_ready_for_development():
    with pytest.raises(AtlError, match="is for Adam"):
        client(FakeTransport(), "malek").jira_flow("DFP-5", "unready")
    transport = FakeTransport((200, json.dumps({"fields": {"status": {"name": "In Development", "statusCategory": {"key": "indeterminate"}}}})))
    with pytest.raises(AtlError, match="only leaves 'Ready for Development'"):
        client(transport, "adam").jira_flow("DFP-5", "unready")
