"""atl jira-edit and jira-link (BASITA-169): every field, every refusal, offline with a fake transport."""

import base64
import io
import json

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main
from atl_agent.client import ADAM_ACCOUNT, AHMED_ONLY_LABELS

TOKEN = "tok-" + "y" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"
EDITED = (204, "")


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        return self.responses.pop(0)


def client(transport, agent="adam"):
    return AtlClient(agent, CLOUD, TOKEN, transport=transport)


def put_body(transport):
    method, url, _, body = transport.calls[-1]
    assert method == "PUT" and url.endswith("/rest/api/3/issue/DFP-1")
    return body


def labels(*names):
    return (200, json.dumps({"fields": {"labels": list(names)}}))


# ---------- each field ----------


def test_add_and_remove_labels_in_one_update():
    transport = FakeTransport(EDITED)
    changes = client(transport).jira_edit("DFP-1", add_labels=("ui", "v2"), remove_labels=("old",))
    assert put_body(transport) == {"update": {"labels": [{"add": "ui"}, {"add": "v2"}, {"remove": "old"}]}}
    assert changes == ["labels +ui +v2 -old"]
    assert len(transport.calls) == 1


def test_set_labels_reads_the_issue_then_replaces_the_list():
    transport = FakeTransport(labels("a"), EDITED)
    assert client(transport).jira_edit("DFP-1", set_labels=("b", "c", "b")) == ["labels = b, c"]
    assert transport.calls[0][0] == "GET" and transport.calls[0][1].endswith("/issue/DFP-1?fields=labels")
    assert put_body(transport) == {"fields": {"labels": ["b", "c"]}}


def test_set_labels_empty_clears_them():
    transport = FakeTransport(labels("a"), EDITED)
    assert client(transport).jira_edit("DFP-1", set_labels=()) == ["labels = (none)"]
    assert put_body(transport) == {"fields": {"labels": []}}


def test_assignee_by_name_and_none():
    transport = FakeTransport(EDITED, EDITED)
    assert client(transport).jira_edit("DFP-1", assignee="Adam") == ["assigned to Adam"]
    assert put_body(transport) == {"fields": {"assignee": {"accountId": ADAM_ACCOUNT}}}
    assert client(transport).jira_edit("DFP-1", assignee="none") == ["unassigned"]
    assert put_body(transport) == {"fields": {"assignee": None}}


def test_description_is_adf():
    transport = FakeTransport(EDITED)
    client(transport).jira_edit("DFP-1", description="line one\n\nline two")
    doc = put_body(transport)["fields"]["description"]
    assert doc["type"] == "doc" and "line two" in json.dumps(doc)


@pytest.mark.parametrize("given, sent", [("High", "High"), ("lowest", "Lowest"), (" MEDIUM ", "Medium")])
def test_priority_by_name_in_any_case(given, sent):
    transport = FakeTransport(EDITED)
    assert client(transport).jira_edit("DFP-1", priority=given) == [f"priority {sent}"]
    assert put_body(transport) == {"fields": {"priority": {"name": sent}}}


def test_fix_versions_add_remove_and_set():
    transport = FakeTransport(EDITED, EDITED, EDITED)
    client(transport).jira_edit("DFP-1", add_fix_versions=("0.3",), remove_fix_versions=("0.2",))
    assert put_body(transport) == {"update": {"fixVersions": [{"add": {"name": "0.3"}}, {"remove": {"name": "0.2"}}]}}
    assert client(transport).jira_edit("DFP-1", set_fix_versions=("0.2", "0.3")) == ["fix versions = 0.2, 0.3"]
    assert put_body(transport) == {"fields": {"fixVersions": [{"name": "0.2"}, {"name": "0.3"}]}}
    client(transport).jira_edit("DFP-1", set_fix_versions=())
    assert put_body(transport) == {"fields": {"fixVersions": []}}


def test_summary_parent_and_due_date():
    transport = FakeTransport(EDITED, EDITED, EDITED)
    client(transport).jira_edit("DFP-1", summary="  New summary ")
    assert put_body(transport) == {"fields": {"summary": "New summary"}}
    client(transport).jira_edit("DFP-1", parent="DFP-7")
    assert put_body(transport) == {"fields": {"parent": {"key": "DFP-7"}}}
    client(transport).jira_edit("DFP-1", duedate="2026-11-30")
    assert put_body(transport) == {"fields": {"duedate": "2026-11-30"}}


def test_due_date_none_clears_it():
    transport = FakeTransport(EDITED)
    assert client(transport).jira_edit("DFP-1", duedate="none") == ["no due date"]
    assert put_body(transport) == {"fields": {"duedate": None}}


def test_several_fields_go_in_one_request():
    transport = FakeTransport(EDITED)
    changes = client(transport).jira_edit("DFP-1", add_labels=("ui",), priority="High", summary="S", duedate="none")
    assert len(transport.calls) == 1
    body = put_body(transport)
    assert body["update"] == {"labels": [{"add": "ui"}]}
    assert body["fields"] == {"priority": {"name": "High"}, "summary": "S", "duedate": None}
    assert changes == ["labels +ui", "priority High", "summary", "no due date"]


# ---------- the labels only Ahmed sets: every form, any case ----------

AHMED_FORMS = [form for label in AHMED_ONLY_LABELS for form in (label, label.upper(), label.title())]


def test_the_ahmed_only_list():
    assert set(AHMED_ONLY_LABELS) == {"reviewed-by-ahmed", "human-reviewed", "needs-ahmed", "jira-status-waived"}


@pytest.mark.parametrize("label", AHMED_FORMS)
def test_adding_an_ahmed_only_label_is_refused_before_any_call(label):
    transport = FakeTransport()
    with pytest.raises(AtlError, match=f"does not add the label '{label}'"):
        client(transport).jira_edit("DFP-1", add_labels=("ok", label))
    assert transport.calls == []


@pytest.mark.parametrize("label", AHMED_FORMS)
def test_removing_an_ahmed_only_label_is_refused_before_any_call(label):
    transport = FakeTransport()
    with pytest.raises(AtlError, match=f"does not remove the label '{label}'"):
        client(transport).jira_edit("DFP-1", remove_labels=(label,))
    assert transport.calls == []


@pytest.mark.parametrize("label", AHMED_FORMS)
def test_a_whole_list_that_adds_an_ahmed_only_label_is_refused(label):
    transport = FakeTransport(labels("ui"))
    with pytest.raises(AtlError, match="would change the labels only Ahmed sets"):
        client(transport).jira_edit("DFP-1", set_labels=("ui", label))
    assert [c[0] for c in transport.calls] == ["GET"]


@pytest.mark.parametrize("label", AHMED_ONLY_LABELS)
def test_a_whole_list_that_drops_an_ahmed_only_label_is_refused(label):
    transport = FakeTransport(labels("ui", label))
    with pytest.raises(AtlError, match=f"only Ahmed sets \\({label} now\\)"):
        client(transport).jira_edit("DFP-1", set_labels=("ui", "new"))
    assert [c[0] for c in transport.calls] == ["GET"]


@pytest.mark.parametrize("label", AHMED_ONLY_LABELS)
def test_a_whole_list_that_changes_an_ahmed_only_labels_case_is_refused(label):
    # Yousef's probe: NEEDS-AHMED for needs-ahmed would replace the label Ahmed set with another string.
    transport = FakeTransport(labels("ui", label))
    with pytest.raises(AtlError, match=f"only Ahmed sets \\({label} now\\)"):
        client(transport).jira_edit("DFP-1", set_labels=(label.upper(), "ui"))
    assert [c[0] for c in transport.calls] == ["GET"]


def test_a_whole_list_that_keeps_them_as_they_are_is_allowed():
    transport = FakeTransport(labels("ui", "reviewed-by-ahmed"), EDITED)
    client(transport).jira_edit("DFP-1", set_labels=("new", "reviewed-by-ahmed"))
    assert put_body(transport) == {"fields": {"labels": ["new", "reviewed-by-ahmed"]}}


@pytest.mark.parametrize("label", ["Jira-Status-Waived", "NEEDS-AHMED", "jira-status-waived"])
def test_create_refuses_them_in_any_case_too(label):
    transport = FakeTransport()
    with pytest.raises(AtlError, match="does not set the label"):
        client(transport, "salma").jira_create("DFP", "Task", "S", "D", "", (label,))
    assert transport.calls == []


# ---------- other refusals, all before any call ----------


@pytest.mark.parametrize(
    "agent, kwargs, message",
    [
        ("adam", {}, "nothing to change"),
        ("adam", {"add_labels": ("two words",)}, "does not add the label"),
        ("adam", {"add_labels": ("",)}, "does not add the label"),
        ("adam", {"set_labels": ("a",), "add_labels": ("b",)}, "not both"),
        ("adam", {"set_fix_versions": ("0.2",), "add_fix_versions": ("0.3",)}, "not both"),
        ("adam", {"add_fix_versions": ("",)}, "not a Fix Version"),
        ("adam", {"set_fix_versions": ("a\nb",)}, "not a Fix Version"),
        ("malek", {"assignee": "adam"}, "does not assign"),
        ("adam", {"assignee": "nobody"}, "no Jira seat"),
        ("adam", {"description": "   "}, "description is empty"),
        ("adam", {"priority": "Urgent"}, "not a priority"),
        ("adam", {"summary": "two\nlines"}, "one line"),
        ("adam", {"summary": "x" * 256}, "one line"),
        ("adam", {"parent": "ELB-1"}, "not another issue in DFP"),
        ("adam", {"parent": "DFP-1"}, "not another issue in DFP"),
        ("adam", {"parent": "not a key"}, "not another issue in DFP"),
        ("adam", {"duedate": "2026-13-01"}, "not a date"),
        ("adam", {"duedate": "03/11/2026"}, "not a date"),
        ("adam", {"duedate": "20261103"}, "not a date"),
    ],
)
def test_edit_refusals(agent, kwargs, message):
    transport = FakeTransport()
    with pytest.raises(AtlError, match=message):
        client(transport, agent).jira_edit("DFP-1", **kwargs)
    assert transport.calls == []


def test_edit_refuses_a_bad_key():
    with pytest.raises(AtlError, match="not an issue key"):
        client(FakeTransport()).jira_edit("dfp 1", summary="S")


@pytest.mark.parametrize("issue", ["HR-7", "BASITAX-1", "DF-1"])
def test_edit_refuses_an_issue_outside_basita_dfp_and_elb(issue):
    # Yousef's probe edited HR-7's summary.
    transport = FakeTransport()
    with pytest.raises(AtlError, match=f"'{issue}' is not in a project an agent edits .*BASITA, DFP, ELB"):
        client(transport).jira_edit(issue, summary="S")
    assert transport.calls == []


@pytest.mark.parametrize("issue", ["BASITA-1", "DFP-1", "ELB-1"])
def test_edit_works_in_basita_dfp_and_elb(issue):
    transport = FakeTransport(EDITED)
    assert client(transport).jira_edit(issue, summary="S") == ["summary"]
    assert transport.calls[0][1].endswith(f"/rest/api/3/issue/{issue}")


# ---------- the CLI ----------


def run(args, transport, token=None, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main([*args, "--cloud-id", CLOUD], token_source=token or (lambda a, p: TOKEN), transport=transport)


def test_cli_edit_signs_the_description_and_says_what_changed(capsys):
    transport = FakeTransport(EDITED)
    args = ["adam", "jira-edit", "DFP-1", "--description", "Given...", "--priority", "high", "--add-label", "ui"]
    assert run(args, transport) == 0
    assert capsys.readouterr().out.strip() == "DFP-1 edited by Adam: labels +ui; description; priority High"
    body = put_body(transport)
    assert "— Adam (Basita AI)" in json.dumps(body["fields"]["description"], ensure_ascii=False)


def test_cli_edit_reads_the_description_from_stdin(monkeypatch):
    transport = FakeTransport(EDITED)
    assert (
        run(
            ["adam", "jira-edit", "DFP-1", "--description", "-"], transport, stdin="from stdin", monkeypatch=monkeypatch
        )
        == 0
    )
    assert "from stdin" in json.dumps(put_body(transport)["fields"]["description"])


def test_cli_edit_lists_and_due_date(capsys):
    transport = FakeTransport(labels(), EDITED)
    args = ["sherif", "jira-edit", "DFP-1", "--set-labels", "a, b", "--set-fix-versions", "0.2", "--due-date", "none"]
    assert run(args, transport) == 0
    assert put_body(transport) == {"fields": {"fixVersions": [{"name": "0.2"}], "duedate": None, "labels": ["a", "b"]}}


def test_cli_edit_as_maya_goes_through_sherif():
    seen = []

    def token(agent, product):
        seen.append((agent, product))
        return TOKEN

    transport = FakeTransport(EDITED)
    assert run(["maya", "--via", "sherif", "jira-edit", "DFP-1", "--summary", "S"], transport, token) == 0
    assert seen == [("sherif", "jira")]
    assert base64.b64decode(transport.calls[0][2]["Authorization"].split()[1]).decode().startswith("sherif@")


def test_cli_edit_refusal_exits_1_and_sends_nothing(capsys):
    transport = FakeTransport()
    assert run(["adam", "jira-edit", "DFP-1", "--add-label", "Reviewed-By-Ahmed"], transport) == 1
    assert "does not add the label 'Reviewed-By-Ahmed'" in capsys.readouterr().err
    assert transport.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["adam", "jira-edit", "DFP-1", "text"],
        ["adam", "jira-edit", "DFP-1", "--label", "ui"],
        ["adam", "jira-edit", "DFP-1", "--link", "DFP-2", "--summary", "S"],
        ["adam", "jira-comment", "DFP-1", "text", "--add-label", "ui"],
        ["adam", "jira-create", "DFP", "Task", "S", "D", "--priority", "High"],
        ["adam", "jira-comment", "DFP-1", "text", "--type", "blocks"],
        ["adam", "jira-link", "DFP-1"],
        ["adam", "jira-link", "DFP-1", "DFP-2", "extra"],
    ],
)
def test_cli_wrong_arguments_exit_2_and_send_nothing(args):
    transport = FakeTransport()
    assert run(args, transport) == 2
    assert transport.calls == []


# ---------- jira-link ----------

TYPES = (
    200,
    json.dumps(
        {
            "issueLinkTypes": [
                {"id": "1", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                {"id": "2", "name": "Relates", "inward": "relates to", "outward": "relates to"},
                {"id": "3", "name": "Duplicate", "inward": "is duplicated by", "outward": "duplicates"},
            ]
        }
    ),
)
CREATED = (201, "")
NONE = (200, json.dumps({"fields": {"issuelinks": []}}))


def links_on(issue_links):
    return (200, json.dumps({"fields": {"issuelinks": issue_links}}))


def shown(type_name, outward, inward, other, as_outward=True):
    side = "outwardIssue" if as_outward else "inwardIssue"
    return {"type": {"name": type_name, "outward": outward, "inward": inward}, side: {"key": other}}


@pytest.mark.parametrize("given", ["blocks", "Blocks", "BLOCKS"])
def test_link_by_name_or_outward_phrase(given):
    transport = FakeTransport(NONE, CREATED, links_on([shown("Blocks", "blocks", "is blocked by", "DFP-2")]))
    assert client(transport).jira_link_typed("DFP-1", "DFP-2", given) == "blocks"
    assert transport.calls[0][1].endswith("/issue/DFP-1?fields=issuelinks")
    method, url, _, body = transport.calls[1]
    assert method == "POST" and url.endswith("/rest/api/3/issueLink")
    assert body == {"type": {"name": "Blocks"}, "inwardIssue": {"key": "DFP-1"}, "outwardIssue": {"key": "DFP-2"}}
    assert transport.calls[2][1].endswith("/issue/DFP-1?fields=issuelinks")


def test_link_by_inward_phrase_reverses_the_issues():
    transport = FakeTransport(
        NONE, CREATED, links_on([shown("Blocks", "blocks", "is blocked by", "DFP-2", as_outward=False)])
    )
    assert client(transport).jira_link_typed("DFP-1", "DFP-2", "is blocked by") == "is blocked by"
    assert transport.calls[1][3]["inwardIssue"] == {"key": "DFP-2"}
    assert transport.calls[1][3]["outwardIssue"] == {"key": "DFP-1"}


def test_link_says_so_when_jira_recorded_it_the_other_way():
    transport = FakeTransport(
        NONE, CREATED, links_on([shown("Blocks", "blocks", "is blocked by", "DFP-2", as_outward=False)])
    )
    with pytest.raises(AtlError, match="Jira recorded DFP-1 is blocked by DFP-2, not DFP-1 blocks DFP-2"):
        client(transport).jira_link_typed("DFP-1", "DFP-2", "blocks")
    assert [c[0] for c in transport.calls] == ["GET", "POST", "GET"]


def test_link_backwards_is_caught_even_when_the_right_one_was_already_there():
    right = shown("Blocks", "blocks", "is blocked by", "DFP-2")
    backwards = shown("Blocks", "blocks", "is blocked by", "DFP-2", as_outward=False)
    transport = FakeTransport(links_on([right]), CREATED, links_on([right, backwards]))
    with pytest.raises(AtlError, match="Jira recorded DFP-1 is blocked by DFP-2, not DFP-1 blocks DFP-2"):
        client(transport).jira_link_typed("DFP-1", "DFP-2", "blocks")


def test_link_already_there_and_unchanged_is_reported_as_it_stands():
    right = shown("Blocks", "blocks", "is blocked by", "DFP-2")
    transport = FakeTransport(links_on([right]), CREATED, links_on([right]))
    assert client(transport).jira_link_typed("DFP-1", "DFP-2", "blocks") == "blocks"


def test_link_not_shown_yet_is_an_error():
    transport = FakeTransport(NONE, CREATED, links_on([shown("Relates", "relates to", "relates to", "DFP-9")]))
    with pytest.raises(AtlError, match="does not show a Blocks link to DFP-2"):
        client(transport).jira_link_typed("DFP-1", "DFP-2", "blocks")


@pytest.mark.parametrize(
    "issue, other, kind, message",
    [
        ("DFP-1", "DFP-1", "blocks", "not linked to itself"),
        ("DFP-1", "dfp 2", "blocks", "not an issue key"),
        ("DFP 1", "DFP-2", "blocks", "not an issue key"),
        ("HR-7", "DFP-2", "blocks", "'HR-7' is not in a project an agent edits"),
        ("DFP-1", "HR-7", "blocks", "'HR-7' is not in a project an agent edits"),
    ],
)
def test_link_refusals_before_any_call(issue, other, kind, message):
    transport = FakeTransport()
    with pytest.raises(AtlError, match=message):
        client(transport).jira_link_typed(issue, other, kind)
    assert transport.calls == []


def test_link_unknown_type_lists_the_known_ones_and_posts_nothing():
    transport = FakeTransport(TYPES)
    with pytest.raises(AtlError, match="'causes' is not a link type .*Blocks \\(blocks / is blocked by\\)"):
        client(transport).jira_link_typed("DFP-1", "DFP-2", "causes")
    assert [c[0] for c in transport.calls] == ["GET"]


def test_cli_link_defaults_to_relates_and_prints_what_jira_shows(capsys):
    transport = FakeTransport(NONE, CREATED, links_on([shown("Relates", "relates to", "relates to", "ELB-3")]))
    assert run(["sherif", "jira-link", "DFP-1", "ELB-3"], transport) == 0
    assert capsys.readouterr().out.strip() == "DFP-1 relates to ELB-3, by Sherif"
    assert transport.calls[1][3]["type"] == {"name": "Relates"}


def test_cli_link_with_type(capsys):
    transport = FakeTransport(NONE, CREATED, links_on([shown("Duplicate", "duplicates", "is duplicated by", "DFP-2")]))
    assert run(["adam", "jira-link", "DFP-1", "DFP-2", "--type", "duplicates"], transport) == 0
    assert capsys.readouterr().out.strip() == "DFP-1 duplicates DFP-2, by Adam"
