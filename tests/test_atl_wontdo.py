"""atl jira-wontdo (BASITA-203): a lead cancels an issue as Won't Do, with a reason. Offline with fakes."""

import io
import json

import pytest
from test_atl_done import (
    CLOUD,
    COMMENTED,
    MINE,
    MOVED,
    NOT_MINE,
    SHERIF,
    TOKEN,
    FakeTransport,
    client,
    comment_text,
    issue,
    no_prs,
    prs,
)

from atl_agent import AtlError
from atl_agent.__main__ import main
from atl_agent.client import JIRA_ACCOUNTS, WONT_DO_LABEL, github_prs

ADAM = JIRA_ACCOUNTS["adam"]
AHMED_WORDS = "دي مش محتاجها ... لو أوريدي رجعت حاجة مرة وكده هبقى واخد بالي إن أنا رجعتها قبل كده"


def moves(*targets):
    """The transitions Jira offers: (id, status name, category)."""
    return (
        200,
        json.dumps(
            {"transitions": [{"id": i, "to": {"name": n, "statusCategory": {"key": c}}} for i, n, c in targets]}
        ),
    )


TODAY = moves(("11", "To Do", "new"), ("21", "In Progress", "indeterminate"), ("31", "Done", "done"))
WITH_STATUS = moves(("11", "To Do", "new"), ("31", "Done", "done"), ("41", "Won’t Do", "done"))
LABELLED = (204, "")


def own(*extra, response=None, transitions=TODAY):
    """The calls of the lead's own case: issue, token user, transitions, comment, move, label."""
    return FakeTransport(response or issue(), MINE, transitions, COMMENTED, MOVED, *extra)


def ahmeds(*extra, response=None, transitions=TODAY):
    """The calls of the Ahmed case: no token-user search."""
    return FakeTransport(response or issue(account=SHERIF), transitions, COMMENTED, MOVED, *extra)


def writes(transport):
    return [(c[0], c[1].split("?")[0], c[3]) for c in transport.calls if c[0] != "GET"]


# ---------- the move ----------


def test_own_issue_without_a_wont_do_status_goes_to_done_with_the_label():
    transport = own(LABELLED)
    assert client(transport).jira_wontdo("BASITA-198", "Ahmed does not need it.", prs=no_prs) == ("Done", True, [])
    assert [(c[0], c[1].split("?")[0]) for c in transport.calls] == [
        ("GET", "/rest/api/3/issue/BASITA-198"),
        ("GET", "/rest/api/3/search/jql"),
        ("GET", "/rest/api/3/issue/BASITA-198/transitions"),
        ("POST", "/rest/api/3/issue/BASITA-198/comment"),
        ("POST", "/rest/api/3/issue/BASITA-198/transitions"),
        ("PUT", "/rest/api/3/issue/BASITA-198"),
    ]
    text = comment_text(transport.calls[3])
    assert "Won't Do: Ahmed does not need it." in text and "No pull request mentions BASITA-198." in text
    assert f"no 'Won't Do' status, so the issue is closed as Done and labelled '{WONT_DO_LABEL}'" in text
    assert "— Malek (Basita AI)" in text
    assert transport.calls[4][3] == {"transition": {"id": "31"}}
    assert transport.calls[5][3] == {"update": {"labels": [{"add": "wont-do"}]}}


def test_uses_the_wont_do_status_when_the_workflow_has_one_and_adds_no_label():
    transport = own(transitions=WITH_STATUS)
    assert client(transport).jira_wontdo("BASITA-198", "Not needed.", prs=no_prs) == ("Won’t Do", False, [])
    assert writes(transport)[-1] == ("POST", "/rest/api/3/issue/BASITA-198/transitions", {"transition": {"id": "41"}})
    assert all(w[0] != "PUT" for w in writes(transport))
    assert "no 'Won't Do' status" not in comment_text(transport.calls[3])


@pytest.mark.parametrize("name", ["Won't Do", "Wont Do", "WON’T DO", "wontdo"])
def test_finds_the_status_by_letters_only(name):
    transport = own(transitions=moves(("31", "Done", "done"), ("41", name, "done")))
    assert client(transport).jira_wontdo("BASITA-198", "Not needed.", prs=no_prs)[1] is False


def test_picks_the_done_category_by_name_then_first_when_the_status_is_missing():
    odd = moves(("77", "Finished", "done"), ("31", "Done", "done"))
    assert client(own(LABELLED, transitions=odd)).jira_wontdo("BASITA-198", "x", prs=no_prs)[0] == "Done"
    only = moves(("77", "Finished", "done"))
    assert client(own(LABELLED, transitions=only)).jira_wontdo("BASITA-198", "x", prs=no_prs)[0] == "Finished"


def test_works_from_to_do_and_from_in_progress():
    for status, category in (("To Do", "new"), ("In Progress", "indeterminate"), ("Refinement", "new")):
        transport = own(LABELLED, response=issue(status, category))
        assert client(transport).jira_wontdo("ELB-3", "Not needed.", prs=no_prs)[0] == "Done"


def test_ahmeds_cancellation_quotes_his_words_and_works_on_anyones_issue():
    transport = ahmeds(LABELLED)
    status, labelled, merged = client(transport, "sherif").jira_wontdo(
        "BASITA-198", "Cancelled by Ahmed.", AHMED_WORDS, no_prs
    )
    assert (status, labelled, merged) == ("Done", True, [])
    assert [(c[0], c[1].split("?")[0]) for c in transport.calls][:2] == [
        ("GET", "/rest/api/3/issue/BASITA-198"),
        ("GET", "/rest/api/3/issue/BASITA-198/transitions"),
    ]  # no token-user search, no ownership test
    lines = [
        "".join(node["text"] for node in paragraph.get("content", []))
        for paragraph in transport.calls[2][3]["body"]["content"]
    ]
    assert f"Cancelled by Ahmed, in his words: \u00ab{AHMED_WORDS}\u00bb" in lines  # verbatim, one paragraph
    text = comment_text(transport.calls[2])
    assert "— Sherif (Basita AI)" in text


def test_ahmeds_cancellation_may_cancel_an_issue_held_for_him():
    transport = ahmeds(LABELLED, response=issue(account=ADAM, labels=["needs-ahmed", "agent-hold"]))
    assert client(transport, "talia").jira_wontdo("BASITA-198", "Cancelled.", "No.", no_prs)[0] == "Done"


def test_merged_prs_may_not_stop_it_and_are_listed():
    transport = own(LABELLED)
    _, _, merged = client(transport).jira_wontdo("BASITA-198", "Not needed.", prs=prs("MERGED", "CLOSED", "MERGED"))
    assert [p["number"] for p in merged] == [1, 3]
    text = comment_text(transport.calls[3])
    assert "https://x/pull/1" in text and "https://x/pull/3" in text and "pull/2" not in text


def test_maya_cancels_her_issue_through_sherif():
    transport = FakeTransport(issue(account=SHERIF, labels=["agent-maya"]), MINE, TODAY, COMMENTED, MOVED, LABELLED)
    assert client(transport, "maya", "sherif").jira_wontdo("BASITA-9", "Not needed.", prs=no_prs)[0] == "Done"
    assert "— Maya (Basita AI, via Sherif)" in comment_text(transport.calls[3])


def test_removes_no_label_and_deletes_nothing():
    transport = own(LABELLED, response=issue(labels=["agent-hold", "agent-picked"]))
    client(transport).jira_wontdo("BASITA-198", "Not needed.", prs=no_prs)
    assert [w[0] for w in writes(transport)] == ["POST", "POST", "PUT"]
    assert "remove" not in json.dumps([w[2] for w in writes(transport)])


# ---------- refusals: nothing is written ----------


def refused(transport, message, agent="malek", via="", key="BASITA-198", source=no_prs, ahmed=""):
    with pytest.raises(AtlError, match=message):
        client(transport, agent, via).jira_wontdo(key, "Not needed.", ahmed, source)
    assert not [c for c in transport.calls if c[0] != "GET"]


@pytest.mark.parametrize(
    "key, message",
    [("DFP-4", "DFP issues move through jira-flow"), ("OTHER-1", "BASITA and ELB issues only"), ("basita-1", "not an")],
)
@pytest.mark.parametrize("ahmed", ["", AHMED_WORDS])
def test_refuses_dfp_and_other_projects_before_any_call(key, message, ahmed):
    transport = FakeTransport()
    refused(transport, message, key=key, ahmed=ahmed)
    assert transport.calls == []


def test_refuses_an_empty_why():
    with pytest.raises(AtlError, match="--why"):
        client(FakeTransport()).jira_wontdo("BASITA-198", "  ", "", no_prs)


@pytest.mark.parametrize("ahmed", ["", AHMED_WORDS])
def test_refuses_an_epic_and_a_closed_issue(ahmed):
    refused(FakeTransport(issue(kind="Epic")), "an Epic", ahmed=ahmed)
    refused(FakeTransport(issue("Done", "done")), "already Done", ahmed=ahmed)
    refused(FakeTransport(issue("Released", "done")), "already Released", ahmed=ahmed)


@pytest.mark.parametrize(
    "response, message",
    [
        (issue(account=SHERIF), "not Malek; jira-wontdo closes only the agent's own issue"),
        (issue(account=None), "assigned to no one"),
        (issue(labels=["needs-ahmed"]), "waits for Ahmed.*--ahmed"),
        (issue(labels=["Needs-Ahmed"]), "waits for Ahmed"),
    ],
)
def test_own_case_refuses_from_the_issue_itself(response, message):
    transport = FakeTransport(response)
    refused(transport, message)
    assert len(transport.calls) == 1


def test_own_case_refuses_when_the_token_is_not_the_assignee():
    refused(FakeTransport(issue(), NOT_MINE), "not assigned to the user whose token this is")


@pytest.mark.parametrize("ahmed", ["", AHMED_WORDS])
def test_refuses_an_open_pr_even_on_ahmeds_word(ahmed):
    first = [issue(), MINE] if not ahmed else [issue(account=SHERIF)]
    transport = FakeTransport(*first)
    refused(
        transport,
        r"an open PR mentions it \(https://x/pull/2\).*close or merge",
        source=prs("MERGED", "OPEN"),
        ahmed=ahmed,
    )


def test_refuses_an_unknown_pr_state():
    refused(FakeTransport(issue(), MINE), "unknown state", source=prs("DRAFT"))


@pytest.mark.parametrize("ahmed", ["", AHMED_WORDS])
def test_fails_closed_when_prs_cannot_be_read(ahmed):
    def broken(key):
        raise AtlError("could not read the pull requests of basitasolutions/basita-ops: auth")

    first = [issue(), MINE] if not ahmed else [issue(account=SHERIF)]
    refused(FakeTransport(*first), "can't tell whether every PR that mentions it is merged", source=broken, ahmed=ahmed)


def test_refuses_without_a_wont_do_or_done_transition():
    only_open = moves(("11", "To Do", "new"))
    refused(FakeTransport(issue(), MINE, only_open), "no transition to Won't Do or to a Done status")


def test_maya_needs_via_sherif():
    refused(FakeTransport(), "run it as atl maya --via sherif jira-done", agent="maya")


def test_sherif_refuses_mayas_issue_and_says_how_to_cancel_it():
    transport = FakeTransport(issue(account=SHERIF, labels=["agent-maya"]))
    refused(transport, "atl maya --via sherif jira-wontdo", agent="sherif")


def test_an_agent_with_no_jira_user_cannot_cancel_its_own_but_can_on_ahmeds_word():
    refused(FakeTransport(), "has no Jira user", agent="talia")
    assert client(ahmeds(LABELLED), "talia").jira_wontdo("BASITA-198", "x", AHMED_WORDS, no_prs)[0] == "Done"


def test_a_failed_move_after_the_comment_says_so():
    transport = FakeTransport(issue(), MINE, TODAY, COMMENTED, (409, "conflict"))
    with pytest.raises(AtlError, match="the comment is posted, but the move to 'Done' failed"):
        client(transport).jira_wontdo("BASITA-198", "x", prs=no_prs)
    assert not any(c[0] == "PUT" for c in transport.calls)


def test_a_failed_label_after_the_move_says_so():
    transport = own((403, "forbidden"))
    with pytest.raises(AtlError, match="moved to 'Done', but the 'wont-do' label failed.*jira-edit"):
        client(transport).jira_wontdo("BASITA-198", "x", prs=no_prs)


def test_fails_closed_while_the_reader_is_pending():
    class Done:
        returncode, stdout, stderr = 1, "", "agent-gh: refused: basita-reader is not set up yet"

    refused(
        FakeTransport(issue(), MINE),
        "can't tell.*basita-reader is not set up yet",
        source=lambda key: github_prs(key, lambda command, **kwargs: Done()),
    )


# ---------- the CLI ----------


def run(args, transport, token=None, source=no_prs, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(
        [*args, "--cloud-id", CLOUD], token_source=token or (lambda a, p: TOKEN), transport=transport, pr_source=source
    )


def test_cli_cancels_and_prints(capsys):
    assert run(["malek", "jira-wontdo", "BASITA-198", "--why", "Not needed."], own(LABELLED)) == 0
    out = capsys.readouterr().out.strip()
    assert out == "BASITA-198 -> Done (no Won't Do status here: labelled wont-do), no PR, by Malek"


def test_cli_prints_the_status_and_ahmeds_word(capsys):
    transport = ahmeds(transitions=WITH_STATUS)
    assert run(["sherif", "jira-wontdo", "BASITA-198", "--why", "W.", "--ahmed", AHMED_WORDS], transport) == 0
    assert capsys.readouterr().out.strip() == "BASITA-198 -> Won’t Do, no PR, on Ahmed's word, by Sherif"


def test_cli_reads_why_from_stdin(monkeypatch):
    transport = own(LABELLED)
    assert (
        run(
            ["malek", "jira-wontdo", "BASITA-198", "--why", "-"],
            transport,
            stdin="From stdin.",
            monkeypatch=monkeypatch,
        )
        == 0
    )
    assert "From stdin." in comment_text(transport.calls[3])


def test_cli_maya_goes_through_sherif():
    seen = []

    def token(agent, product):
        seen.append((agent, product))
        return TOKEN

    transport = FakeTransport(issue(account=SHERIF, labels=["agent-maya"]), MINE, TODAY, COMMENTED, MOVED, LABELLED)
    assert run(["maya", "--via", "sherif", "jira-wontdo", "BASITA-9", "--why", "W."], transport, token) == 0
    assert seen == [("sherif", "jira")]


def test_cli_refusal_exits_1(capsys):
    transport = FakeTransport()
    assert run(["malek", "jira-wontdo", "DFP-1", "--why", "W."], transport) == 1
    assert "jira-flow" in capsys.readouterr().err and transport.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["malek", "jira-wontdo", "BASITA-7"],
        ["malek", "jira-wontdo", "BASITA-7", "--why", " "],
        ["malek", "jira-wontdo", "BASITA-7", "text", "--why", "W."],
        ["malek", "jira-wontdo", "BASITA-7", "--why", "W.", "--ahmed", " "],
        ["malek", "jira-wontdo", "BASITA-7", "--why", "W.", "--add-label", "x"],
        ["malek", "jira-wontdo", "BASITA-7", "--why", "W.", "--assignee", "adam"],
        ["malek", "jira-done", "BASITA-7", "--why", "W.", "--ahmed", "words"],
        ["malek", "jira-comment", "BASITA-7", "text", "--ahmed", "words"],
        ["malek", "jira-comment", "BASITA-7", "text", "--why", "W."],
    ],
)
def test_cli_wrong_arguments_exit_2_and_send_nothing(args):
    transport = FakeTransport()
    assert run(args, transport) == 2
    assert transport.calls == []
