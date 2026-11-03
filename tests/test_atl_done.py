"""atl jira-done (BASITA-178): an agent's own BASITA or ELB issue, In Progress to Done, offline with fakes."""

import base64
import io
import json
import os
import subprocess

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main
from atl_agent.client import AGENT_GH, JIRA_ACCOUNTS, PR_LIST_LIMIT, PR_REPOS, github_prs, mentions

TOKEN = "tok-" + "d" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"
MALEK = JIRA_ACCOUNTS["malek"]
SHERIF = JIRA_ACCOUNTS["sherif"]


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split(CLOUD, 1)[1], headers, json.loads(body) if body else None))
        return self.responses.pop(0)


def issue(status="In Progress", category="indeterminate", account=MALEK, labels=(), kind="Task"):
    fields = {
        "status": {"name": status, "statusCategory": {"key": category}},
        "assignee": {"accountId": account, "displayName": "Someone"} if account else None,
        "labels": list(labels),
        "issuetype": {"name": kind},
    }
    return (200, json.dumps({"fields": fields}))


MINE = (200, json.dumps({"issues": [{"key": "BASITA-7"}]}))
NOT_MINE = (200, json.dumps({"issues": []}))
TRANSITIONS = (
    200,
    json.dumps(
        {
            "transitions": [
                {"id": "11", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
                {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
            ]
        }
    ),
)
COMMENTED = (201, "{}")
MOVED = (204, "")


def no_prs(key):
    return []


def prs(*states):
    def source(key):
        return [
            {"repo": "basitasolutions/basita-ops", "number": n, "state": s, "url": f"https://x/pull/{n}"}
            for n, s in enumerate(states, start=1)
        ]

    return source


def client(transport, agent="malek", via=""):
    return AtlClient(agent, CLOUD, TOKEN, transport=transport, via=via)


def happy(*extra):
    return FakeTransport(issue(), MINE, TRANSITIONS, COMMENTED, MOVED, *extra)


def comment_text(call):
    return json.dumps(call[3]["body"], ensure_ascii=False)


# ---------- the move ----------


def test_comments_then_moves_to_the_done_category():
    transport = happy()
    assert client(transport).jira_done("BASITA-7", "Page 123 written.", no_prs) == ("Done", [])
    methods = [(c[0], c[1].split("?")[0]) for c in transport.calls]
    assert methods == [
        ("GET", "/rest/api/3/issue/BASITA-7"),
        ("GET", "/rest/api/3/search/jql"),
        ("GET", "/rest/api/3/issue/BASITA-7/transitions"),
        ("POST", "/rest/api/3/issue/BASITA-7/comment"),
        ("POST", "/rest/api/3/issue/BASITA-7/transitions"),
    ]
    text = comment_text(transport.calls[3])
    assert "Done: Page 123 written." in text and "No pull request mentions BASITA-7." in text
    assert "— Malek (Basita AI)" in text
    assert transport.calls[4][3] == {"transition": {"id": "31"}}


def test_picks_the_transition_by_category_not_by_name():
    moves = (
        200,
        json.dumps({"transitions": [{"id": "77", "to": {"name": "Finished", "statusCategory": {"key": "done"}}}]}),
    )
    transport = FakeTransport(issue(), MINE, moves, COMMENTED, MOVED)
    assert client(transport).jira_done("ELB-3", "Delivered.", no_prs)[0] == "Finished"
    assert transport.calls[-1][3] == {"transition": {"id": "77"}}


def test_all_merged_prs_may_close_and_are_listed():
    transport = happy()
    status, merged = client(transport).jira_done("BASITA-7", "Docs too.", prs("MERGED", "CLOSED", "MERGED"))
    assert status == "Done" and [p["number"] for p in merged] == [1, 3]
    text = comment_text(transport.calls[3])
    assert "https://x/pull/1" in text and "https://x/pull/3" in text and "pull/2" not in text


def test_never_touches_labels_or_deletes():
    transport = happy()
    client(transport).jira_done("BASITA-7", "Done.", no_prs)
    assert all(c[0] in ("GET", "POST") for c in transport.calls)
    assert not any("labels" in json.dumps(c[3] or {}) for c in transport.calls if c[0] == "POST")


def test_maya_closes_her_issue_through_sherif():
    transport = FakeTransport(issue(account=SHERIF, labels=["agent-maya"]), MINE, TRANSITIONS, COMMENTED, MOVED)
    assert client(transport, "maya", "sherif").jira_done("BASITA-7", "Rules page.", no_prs)[0] == "Done"
    assert "— Maya (Basita AI, via Sherif)" in comment_text(transport.calls[3])


def test_sherif_closes_his_own_issue():
    transport = FakeTransport(issue(account=SHERIF), MINE, TRANSITIONS, COMMENTED, MOVED)
    assert client(transport, "sherif").jira_done("ELB-2", "Plan.", no_prs)[0] == "Done"


# ---------- refusals: nothing is written ----------


def refused(transport, message, agent="malek", via="", key="BASITA-7", source=no_prs):
    with pytest.raises(AtlError, match=message):
        client(transport, agent, via).jira_done(key, "Delivered.", source)
    assert not [c for c in transport.calls if c[0] != "GET"]


@pytest.mark.parametrize(
    "key, message",
    [("DFP-4", "DFP issues move through jira-flow"), ("OTHER-1", "BASITA and ELB issues only"), ("basita-1", "not an")],
)
def test_refuses_dfp_and_other_projects_before_any_call(key, message):
    transport = FakeTransport()
    refused(transport, message, key=key)
    assert transport.calls == []


def test_refuses_an_empty_why():
    with pytest.raises(AtlError, match="--why"):
        client(FakeTransport()).jira_done("BASITA-7", "  ", no_prs)


@pytest.mark.parametrize(
    "response, message",
    [
        (issue(account=SHERIF), "not Malek; jira-done closes only the agent's own issue"),
        (issue(account=None), "assigned to no one"),
        (issue(kind="Epic"), "an Epic"),
        (issue("To Do", "new"), "only closes an issue In Progress"),
        (issue("Done", "done"), "already Done"),
        (issue(labels=["needs-ahmed"]), "waits for Ahmed"),
        (issue(labels=["Needs-Ahmed"]), "waits for Ahmed"),
    ],
)
def test_refuses_from_the_issue_itself(response, message):
    transport = FakeTransport(response)
    refused(transport, message)
    assert len(transport.calls) == 1


def test_refuses_when_the_token_is_not_the_assignee():
    transport = FakeTransport(issue(), NOT_MINE)
    refused(transport, "not assigned to the user whose token this is")


def test_refuses_an_open_pr():
    transport = FakeTransport(issue(), MINE)
    refused(transport, r"an open PR mentions it \(https://x/pull/2\).*Closes BASITA-7", source=prs("MERGED", "OPEN"))


def test_refuses_an_unknown_pr_state():
    transport = FakeTransport(issue(), MINE)
    refused(transport, "unknown state", source=prs("DRAFT"))


def test_fails_closed_when_prs_cannot_be_read():
    def broken(key):
        raise AtlError("could not read the pull requests of basitasolutions/basita-ops: auth")

    transport = FakeTransport(issue(), MINE)
    refused(transport, "can't tell whether every PR that mentions it is merged", source=broken)


def test_refuses_without_a_done_transition():
    moves = (
        200,
        json.dumps({"transitions": [{"id": "11", "to": {"name": "To Do", "statusCategory": {"key": "new"}}}]}),
    )
    transport = FakeTransport(issue(), MINE, moves)
    refused(transport, "no transition to a Done status")


def test_maya_needs_via_sherif():
    refused(FakeTransport(), "run it as atl maya --via sherif jira-done", agent="maya")


def test_maya_refuses_sherifs_own_issue():
    transport = FakeTransport(issue(account=SHERIF))
    refused(transport, "no 'agent-maya' label", agent="maya", via="sherif")


def test_sherif_refuses_mayas_issue_and_says_how():
    transport = FakeTransport(issue(account=SHERIF, labels=["agent-maya"]))
    refused(transport, "atl maya --via sherif jira-done", agent="sherif")


def test_another_agent_through_via_is_refused():
    refused(FakeTransport(), "without --via", agent="malek", via="sherif")


def test_an_agent_with_no_jira_user_is_refused():
    refused(FakeTransport(), "has no Jira user", agent="talia")


def test_a_failed_move_after_the_comment_says_so():
    transport = FakeTransport(issue(), MINE, TRANSITIONS, COMMENTED, (409, "conflict"))
    with pytest.raises(AtlError, match="the comment is posted, but the move to 'Done' failed"):
        client(transport).jira_done("BASITA-7", "Delivered.", no_prs)


# ---------- reading the PRs ----------


def test_mentions_follows_jira_attention():
    assert mentions({"title": "BASITA-7: page"}, "BASITA-7")
    assert mentions({"headRefName": "malek/basita-7-x"}, "BASITA-7")
    assert mentions({"body": "Refs BASITA-7"}, "BASITA-7")
    assert mentions({"body": "closes: basita-7"}, "BASITA-7")
    assert not mentions({"title": "BASITA-78"}, "BASITA-7")
    assert not mentions({"body": "see BASITA-7"}, "BASITA-7")  # a plain mention in the body doesn't count
    assert not mentions({"body": "<!-- Closes BASITA-7 -->"}, "BASITA-7")


class Done:
    def __init__(self, returncode=0, stdout="[]", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_github_prs_reads_every_repo_as_the_reader_and_keeps_the_mentions():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        repo = command[command.index("-R") + 1]
        pr = {"number": 5, "title": "BASITA-7 x" if "ops" in repo else "other", "state": "MERGED", "url": "u"}
        return Done(stdout=json.dumps([pr]))

    found = github_prs("BASITA-7", run)
    assert [c[:4] for c in calls] == [[AGENT_GH, "reader", "pr", "list"]] * len(PR_REPOS)
    assert [c[c.index("-R") + 1] for c in calls] == list(PR_REPOS)
    assert all(c[c.index("--state") + 1] == "all" for c in calls)
    assert not any(c[0] == "gh" for c in calls)  # never the Mac's own gh login
    assert found == [{"repo": "basitasolutions/basita-ops", "number": 5, "state": "MERGED", "url": "u"}]


def test_agent_gh_is_the_playbooks_own_script():
    assert AGENT_GH.endswith("/scripts/agent-gh") and os.path.isabs(AGENT_GH) and os.path.isfile(AGENT_GH)


@pytest.mark.parametrize(
    "answer, message",
    [
        (Done(returncode=1, stderr="HTTP 401"), "HTTP 401"),
        (Done(returncode=1, stderr="agent-gh: refused: basita-reader is not set up yet (S2)"), "not set up yet"),
        (Done(stdout="not json"), "did not answer with JSON"),
        (Done(stdout="{}"), "did not answer with a list"),
        (Done(stdout=json.dumps([{"number": n} for n in range(PR_LIST_LIMIT)])), "more than atl reads"),
    ],
)
def test_github_prs_fails_closed(answer, message):
    with pytest.raises(AtlError, match=message):
        github_prs("BASITA-7", lambda command, **kwargs: answer)


def test_github_prs_fails_closed_without_gh():
    def run(command, **kwargs):
        raise FileNotFoundError("gh")

    with pytest.raises(AtlError, match="could not read"):
        github_prs("BASITA-7", run)

    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 120)

    with pytest.raises(AtlError, match="could not read"):
        github_prs("BASITA-7", slow)


# ---------- the CLI ----------


def run(args, transport, token=None, source=no_prs, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return main(
        [*args, "--cloud-id", CLOUD], token_source=token or (lambda a, p: TOKEN), transport=transport, pr_source=source
    )


def test_cli_closes_and_prints(capsys):
    transport = happy()
    assert run(["malek", "jira-done", "BASITA-7", "--why", "Runbook page."], transport) == 0
    assert capsys.readouterr().out.strip() == "BASITA-7 -> Done, no PR, by Malek"


def test_cli_reads_why_from_stdin(monkeypatch):
    transport = happy()
    assert (
        run(["malek", "jira-done", "BASITA-7", "--why", "-"], transport, stdin="From stdin.", monkeypatch=monkeypatch)
        == 0
    )
    assert "From stdin." in comment_text(transport.calls[3])


def test_cli_maya_goes_through_sherif():
    seen = []

    def token(agent, product):
        seen.append((agent, product))
        return TOKEN

    transport = FakeTransport(issue(account=SHERIF, labels=["agent-maya"]), MINE, TRANSITIONS, COMMENTED, MOVED)
    assert run(["maya", "--via", "sherif", "jira-done", "BASITA-7", "--why", "W."], transport, token) == 0
    assert seen == [("sherif", "jira")]
    assert base64.b64decode(transport.calls[0][2]["Authorization"].split()[1]).decode().startswith("sherif@")


def test_cli_refusal_exits_1(capsys):
    transport = FakeTransport()
    assert run(["malek", "jira-done", "DFP-1", "--why", "W."], transport) == 1
    assert "jira-flow" in capsys.readouterr().err and transport.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["malek", "jira-done", "BASITA-7"],
        ["malek", "jira-done", "BASITA-7", "--why", " "],
        ["malek", "jira-done", "BASITA-7", "text", "--why", "W."],
        ["malek", "jira-done", "BASITA-7", "--why", "W.", "--add-label", "x"],
        ["malek", "jira-done", "BASITA-7", "--why", "W.", "--mention", "acc"],
        ["malek", "jira-done", "BASITA-7", "--why", "W.", "--assignee", "adam"],
        ["malek", "jira-comment", "BASITA-7", "text", "--why", "W."],
        ["malek", "jira-start", "BASITA-7", "--why", "W."],
    ],
)
def test_cli_wrong_arguments_exit_2_and_send_nothing(args):
    transport = FakeTransport()
    assert run(args, transport) == 2
    assert transport.calls == []


def test_jira_done_fails_closed_while_the_reader_is_pending():
    def pending(command, **kwargs):
        return Done(returncode=1, stderr="agent-gh: refused: basita-reader is not set up yet (Ahmed's setup S2)")

    transport = FakeTransport(issue(), MINE)
    refused(transport, "can't tell.*basita-reader is not set up yet", source=lambda key: github_prs(key, pending))
