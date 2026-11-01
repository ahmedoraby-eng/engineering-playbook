import base64
import io
import json

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main as cli
from atl_agent.close import DEPLOY_WAIT_TRIES, close_issues, closing_keys, deploy_state, main, wait_for_deploy

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
    first = "".join(n["text"] for n in comment[0]["content"])  # the PR URL is a link node now
    assert "https://github.com/o/r/pull/7 was merged" in first
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


DFP_MOVES = (
    ("31", "Released", "done"),
    ("41", "Closed", "done"),
    ("7", "Ready for Release", "indeterminate"),
)


def test_dfp_moves_from_ready_for_release_to_released():
    transport = FakeTransport(
        status("Ready for Release", "indeterminate"),
        transitions(*DFP_MOVES),
        (204, ""),
        (201, json.dumps({"id": "5"})),
    )
    assert close_issues(client(transport), ["DFP-1"], "http://pr/1") == ["DFP-1: Ready for Release -> Released"]
    assert transport.calls[2][1].endswith("/transitions") and transport.calls[2][2] == {"transition": {"id": "31"}}
    assert "Moved to Released automatically" in json.dumps(transport.calls[3][2])


def test_dfp_never_picks_closed_even_when_it_comes_first():
    transport = FakeTransport(
        status("Ready for Release", "indeterminate"),
        transitions(("41", "Closed", "done"), ("31", "Released", "done")),
        (204, ""),
        (201, json.dumps({"id": "5"})),
    )
    close_issues(client(transport), ["DFP-1"], "http://pr/1")
    assert transport.calls[2][2] == {"transition": {"id": "31"}}


def test_dfp_with_no_released_transition_is_an_error_and_moves_nothing():
    transport = FakeTransport(status("Ready for Release", "indeterminate"), transitions(("41", "Closed", "done")))
    with pytest.raises(AtlError, match="'Released'"):
        close_issues(client(transport), ["DFP-1"], "http://pr/1")
    assert all(method == "GET" for method, _, _ in transport.calls)


@pytest.mark.parametrize(
    "name, category",
    [
        ("Refinement", "new"),
        ("Ready for Development", "new"),
        ("In Development", "indeterminate"),
        ("Ready for Testing", "indeterminate"),
        ("In Testing", "indeterminate"),
        ("Ready for Acceptance", "indeterminate"),
    ],
)
def test_dfp_from_any_other_status_only_comments(name, category):
    transport = FakeTransport(status(name, category), (201, json.dumps({"id": "5"})))
    lines = close_issues(client(transport), ["DFP-1"], "http://pr/1")
    assert lines == [f"DFP-1: left in {name}, a merge moves it only from Ready for Release"]
    assert len(transport.calls) == 2 and transport.calls[1][0] == "POST" and transport.calls[1][1].endswith("/comment")
    text = json.dumps(transport.calls[1][2])
    assert name in text and "http://pr/1" in text and "Not moved automatically" in text


@pytest.mark.parametrize("name", ["Released", "Closed"])
def test_dfp_already_done_is_left_alone(name):
    transport = FakeTransport(status(name, "done"))
    assert close_issues(client(transport), ["DFP-1"], "http://pr/1") == [f"DFP-1: already {name}, nothing to do"]
    assert len(transport.calls) == 1


@pytest.mark.parametrize("key", ["BASITA-1", "ELB-1"])
def test_basita_and_elb_still_close_from_any_status(key):
    transport = FakeTransport(
        status("In Progress", "indeterminate"), transitions(("31", "Done", "done")), (204, ""), (201, "{}")
    )
    assert close_issues(client(transport), [key], "http://pr/1") == [f"{key}: In Progress -> Done"]
    assert "Closed automatically" in json.dumps(transport.calls[3][2])


def test_the_default_account_is_sherifs():
    logins = []

    def transport(method, url, headers, body):
        logins.append(base64.b64decode(headers["Authorization"].split()[1]).decode())
        return status("Done", "done")

    env = {"ATL_TOKEN": TOKEN, "ATL_CLOUD_ID": "cloud-1"}
    assert main(["--pr-url", "u"], stdin=io.StringIO("Closes ELB-1"), environ=env, transport=transport) == 0
    assert logins[0].startswith("sherif@")


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
        (issue("In Review", "indeterminate"), "only starts issues in To Do"),
    ],
)
def test_start_refuses_a_second_pickup_or_a_started_issue(response, message):
    transport = FakeTransport(response)
    with pytest.raises(AtlError, match=message):
        client(transport).jira_start("DFP-12")
    assert len(transport.calls) == 1  # nothing written


def test_start_without_in_progress_transition_writes_nothing():
    transport = FakeTransport(issue("To Do", "new"), transitions(("31", "Done", "done")))
    with pytest.raises(AtlError, match="no transition to an In Progress"):
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


CHECK = "Workers Builds: app"


def fake_api(later=(), **checks):
    """GitHub answers: `later` commits after the merge, and each commit's check as (status, conclusion)."""

    def api(path):
        if "/compare/" in path:
            return {"commits": [{"sha": sha} for sha in later]}
        commit = path.split("/commits/")[1].split("/")[0]
        assert "check_name=Workers%20Builds%3A%20app" in path
        return {"check_runs": [{"status": s, "conclusion": c} for s, c in checks.get(commit, [])]}

    return api


@pytest.mark.parametrize(
    ("later", "checks", "expected"),
    [
        ((), {"m": [("completed", "success")]}, "success"),
        ((), {"m": [("completed", "failure")]}, "failed"),
        ((), {"m": [("completed", "skipped")]}, "failed"),
        ((), {"m": [("in_progress", None)]}, "pending"),
        ((), {}, "pending"),  # no check yet, or none at all
        (("n",), {"n": [("completed", "success")]}, "success"),  # Cloudflare built only the newest of quick merges
        (("n",), {"m": [("completed", "failure")], "n": [("completed", "success")]}, "success"),
        (("n",), {"m": [("completed", "failure")]}, "pending"),  # the next build may still contain it
        (("n",), {"m": [("completed", "failure")], "n": [("completed", "failure")]}, "pending"),
    ],
)
def test_deploy_state(later, checks, expected):
    assert deploy_state(fake_api(later, **checks), "o/r", "m", "main", CHECK) == expected


def test_wait_stops_at_a_result_and_gives_up_when_nothing_comes():
    naps = []
    answers = iter(["pending", "pending", "success"])
    assert wait_for_deploy(lambda: next(answers), naps.append) is True
    assert len(naps) == 2
    assert wait_for_deploy(lambda: "failed", naps.append) is False
    assert len(naps) == 2
    assert wait_for_deploy(lambda: "pending", naps.append) is False
    assert len(naps) == 2 + DEPLOY_WAIT_TRIES - 1


def test_a_guarded_issue_stays_when_the_deploy_is_not_confirmed():
    transport = FakeTransport(status("Ready for Release", "indeterminate"), (201, "{}"))
    lines = close_issues(client(transport), ["DFP-1"], "u", deployed=lambda: False, deploy_check=CHECK)
    assert lines == ["DFP-1: left in Ready for Release, deploy not confirmed"]
    assert [call[0] for call in transport.calls] == ["GET", "POST"]  # a comment, no transition
    assert "/comment" in transport.calls[1][1]


def test_a_guarded_issue_moves_once_the_deploy_is_confirmed():
    transport = FakeTransport(
        status("Ready for Release", "indeterminate"),
        transitions(("9", "Released", "done")),
        (204, ""),
        (201, "{}"),
    )
    lines = close_issues(client(transport), ["DFP-1"], "u", deployed=lambda: True, deploy_check=CHECK)
    assert lines == ["DFP-1: Ready for Release -> Released"]


def test_the_wait_is_skipped_for_an_issue_the_guard_would_not_move_and_for_others():
    asked = []
    transport = FakeTransport(
        status("In Testing", "indeterminate"),
        (201, "{}"),
        status("To Do", "new"),
        transitions(("9", "Done", "done")),
        (204, ""),
        (201, "{}"),
    )
    deployed = lambda: asked.append(1) or True  # noqa: E731
    close_issues(client(transport), ["DFP-1", "ELB-2"], "u", deployed=deployed, deploy_check=CHECK)
    assert asked == []


def test_two_issues_ask_about_the_deploy_once():
    asked = []
    transport = FakeTransport(
        status("Ready for Release", "indeterminate"),
        (201, "{}"),
        status("Ready for Release", "indeterminate"),
        (201, "{}"),
    )
    deployed = lambda: asked.append(1) or False  # noqa: E731
    close_issues(client(transport), ["DFP-1", "DFP-2"], "u", deployed=deployed, deploy_check=CHECK)
    assert asked == [1]


def test_main_fails_the_job_when_the_deploy_is_not_confirmed(capsys):
    transport = FakeTransport(status("Ready for Release", "indeterminate"), (201, "{}"))
    env = {"ATL_TOKEN": TOKEN, "ATL_CLOUD_ID": "cloud-1"}
    argv = ["--pr-url", "u", "--deploy-check", CHECK, "--repo", "o/r", "--sha", "m"]
    api = fake_api(m=[("completed", "failure")])
    assert main(argv, stdin=io.StringIO("Closes DFP-1"), environ=env, transport=transport, api=api) == 1
    assert "deploy not confirmed" in capsys.readouterr().out


def test_main_without_a_deploy_check_never_asks_github():
    transport = FakeTransport(
        status("Ready for Release", "indeterminate"),
        transitions(("9", "Released", "done")),
        (204, ""),
        (201, "{}"),
    )
    env = {"ATL_TOKEN": TOKEN, "ATL_CLOUD_ID": "cloud-1"}

    def no_api(path):
        raise AssertionError(path)

    assert main(["--pr-url", "u"], stdin=io.StringIO("Closes DFP-1"), environ=env, transport=transport, api=no_api) == 0
