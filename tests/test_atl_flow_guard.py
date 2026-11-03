"""BASITA-151 (plan 4.3, T3): the approve row and the review guard on submit and approve.

Fakes only: no Jira, no GitHub.
"""

import json
import subprocess

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.client import (
    FLOW,
    SALMA_ACCOUNT,
    arch_review_state,
    flow_settings,
    is_architecture_review,
    review_guard,
)

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"
HEAD = "a" * 40
OLD = "b" * 40
ON = {"FLOW_REVIEW_GUARD": "on", "FLOW_APPROVE": "on"}
OFF = {"FLOW_REVIEW_GUARD": "off", "FLOW_APPROVE": "off"}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        return self.responses.pop(0) if self.responses else (200, "{}")


def _status(name):
    return (200, json.dumps({"fields": {"status": {"name": name, "statusCategory": {"key": "indeterminate"}}}}))


def _moves():
    return (200, json.dumps({"transitions": [{"id": "1", "to": {"name": "Ready for Testing"}}]}))


def moving():
    """Jira answers for one step that goes through: status, transitions, assign, move."""
    return FakeTransport(_status("In Development"), _moves(), (204, ""), (204, ""))


def client(transport, agent):
    return AtlClient(agent, CLOUD, TOKEN, transport=transport)


def pr(state="OPEN", number=7):
    return {
        "repo": "basitasolutions/elbayoumy-farm-nutrition-platform",
        "number": number,
        "state": state,
        "url": f"u#{number}",
    }


def review(state="green", checked=HEAD, head=HEAD):
    return lambda repo, number: {"state": state, "head": head, "checked": checked, "class": "normal"}


def no_github(*_):
    raise AssertionError("GitHub was read")


@pytest.fixture(autouse=True)
def no_real_conf(tmp_path, monkeypatch):
    """The switches never come from the Mac's real jira-agents.conf in a test."""
    monkeypatch.setenv("ATL_AGENTS_CONF", str(tmp_path / "missing.conf"))


# --- the approve row ----------------------------------------------------------------------------------------------


def test_approve_is_a_row_of_the_flow_behind_its_switch():
    step = FLOW["approve"]
    assert step["agents"] == ("yousef",) and step["from"] == "In Development" and step["to"] == "Ready for Testing"
    assert step["assign"] == SALMA_ACCOUNT and step["switch"] == "FLOW_APPROVE"


def test_approve_is_refused_while_its_switch_is_off_before_any_call():
    transport = FakeTransport()
    with pytest.raises(AtlError, match=r"approve is off \(FLOW_APPROVE"):
        client(transport, "yousef").jira_flow("DFP-1", "approve", no_github, no_github, OFF)
    assert transport.calls == []
    with pytest.raises(AtlError, match="approve is off"):  # the default: no conf file at all
        client(FakeTransport(), "yousef").jira_flow("DFP-1", "approve", no_github, no_github)


def test_approve_with_a_green_review_moves_to_testing_and_assigns_salma():
    transport = moving()
    approve_only = {"FLOW_REVIEW_GUARD": "off", "FLOW_APPROVE": "on"}
    end = client(transport, "yousef").jira_flow("DFP-1", "approve", lambda key: [pr()], review("green"), approve_only)
    assert end == "Ready for Testing"
    assert transport.calls[2][3] == {"accountId": SALMA_ACCOUNT} and transport.calls[3][0] == "POST"


@pytest.mark.parametrize(
    ("prs", "state", "why"),
    [
        ([], "green", "no open PR mentions it"),
        ([pr(state="MERGED")], "green", "no open PR mentions it"),
        ([pr()], "waiting", "Architecture review is waiting"),
        ([pr()], "changes", "Architecture review is changes"),
    ],
)
def test_approve_refusals_write_nothing(prs, state, why):
    transport = FakeTransport(_status("In Development"))
    with pytest.raises(AtlError, match=why):
        client(transport, "yousef").jira_flow("DFP-1", "approve", lambda key: prs, review(state), ON)
    assert [c[0] for c in transport.calls] == ["GET"]


def test_approve_is_yousefs_only():
    with pytest.raises(AtlError, match="approve is for Yousef"):
        client(FakeTransport(), "malek").jira_flow("DFP-1", "approve", no_github, no_github, ON)


# --- the guard on submit -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "checked", "moves"),
    [
        ("green", HEAD, True),  # reviewed and approved on the head
        ("green", OLD, False),  # green, but on an older commit
        ("waiting", HEAD, False),  # red: the reviewer's turn
        ("changes", HEAD, False),  # red: changes requested
        ("error", HEAD, False),
        ("missing", "", False),
        ("running", "", False),
        ("unreadable", "", False),
    ],
)
def test_submit_with_the_guard_on(state, checked, moves):
    transport = moving() if moves else FakeTransport(_status("In Development"))
    run = client(transport, "malek").jira_flow
    if moves:
        assert run("DFP-1", "submit", lambda key: [pr()], review(state, checked), ON) == "Ready for Testing"
    else:
        with pytest.raises(AtlError, match="submit waits for the architect's review"):
            run("DFP-1", "submit", lambda key: [pr()], review(state, checked), ON)
        assert [c[0] for c in transport.calls] == ["GET"]


def test_submit_with_a_not_required_review_works_as_before():
    """Architecture review reports a PR that needs no review as state=green (class=not-required)."""
    not_required = lambda repo, number: {"state": "green", "head": HEAD, "checked": HEAD, "class": "not-required"}  # noqa: E731
    assert (
        client(moving(), "malek").jira_flow("DFP-1", "submit", lambda key: [pr()], not_required, ON)
        == "Ready for Testing"
    )


def test_submit_with_no_open_pr_works_as_before():
    """Work that isn't code (Nora, Sherif) has no PR; a merged or closed PR doesn't hold it either."""
    prs = lambda key: [pr(state="MERGED"), pr(state="CLOSED", number=8)]  # noqa: E731
    assert client(moving(), "nora").jira_flow("DFP-1", "submit", prs, no_github, ON) == "Ready for Testing"


def test_submit_checks_every_open_pr():
    states = {7: "green", 8: "waiting"}
    each = lambda repo, number: {"state": states[number], "head": HEAD, "checked": HEAD}  # noqa: E731
    with pytest.raises(AtlError, match=r"u#8: Architecture review is waiting") as caught:
        client(FakeTransport(_status("In Development")), "malek").jira_flow(
            "DFP-1", "submit", lambda key: [pr(), pr(number=8)], each, ON
        )
    assert "u#7" not in str(caught.value)


def test_submit_with_the_guard_off_reads_no_github():
    assert client(moving(), "malek").jira_flow("DFP-1", "submit", no_github, no_github, OFF) == "Ready for Testing"


def test_a_pr_list_or_review_that_cant_be_read_refuses():
    def broken(key):
        raise AtlError("agent-gh refused")

    def unreadable(repo, number):
        raise AtlError("HTTP 502")

    for prs, check, why in (
        (broken, no_github, "can't tell which PRs"),
        (lambda k: [pr()], unreadable, "can't be read"),
    ):
        with pytest.raises(AtlError, match=why):
            client(FakeTransport(_status("In Development")), "malek").jira_flow("DFP-1", "submit", prs, check, ON)


def test_the_guard_runs_after_the_status_check():
    transport = FakeTransport(_status("Ready for Testing"))
    with pytest.raises(AtlError, match="only leaves"):
        client(transport, "malek").jira_flow("DFP-1", "submit", no_github, no_github, ON)


def test_review_guard_alone():
    assert review_guard("DFP-1", "submit", lambda k: [], no_github) == []
    assert review_guard("DFP-1", "submit", lambda k: [pr()], review("green")) == []
    assert review_guard("DFP-1", "approve", lambda k: [], no_github) == [
        "no open PR mentions it, so there is no review to approve"
    ]


# --- the switches -------------------------------------------------------------------------------------------------


def test_switches_default_off_and_read_the_conf_without_running_it(tmp_path):
    assert flow_settings(tmp_path / "none.conf") == OFF
    conf = tmp_path / "jira-agents.conf"
    conf.write_text(
        '# FLOW_REVIEW_GUARD="on" in a comment is ignored\n'
        'FLOW_REVIEW_GUARD="off"\n'
        "FLOW_REVIEW_GUARD=on  # last wins\n"
        'OTHER="x"\n'
    )
    assert flow_settings(conf) == {"FLOW_REVIEW_GUARD": "on", "FLOW_APPROVE": "off"}
    conf.write_text('FLOW_APPROVE="$(touch /tmp/x)"\n')
    with pytest.raises(AtlError, match="FLOW_APPROVE .* is '\\$\\(touch /tmp/x\\)', not off or on"):
        flow_settings(conf)


def test_the_switches_come_from_atl_agents_conf(tmp_path, monkeypatch):
    conf = tmp_path / "c.conf"
    conf.write_text('FLOW_APPROVE="on"\n')
    monkeypatch.setenv("ATL_AGENTS_CONF", str(conf))
    assert flow_settings()["FLOW_APPROVE"] == "on"
    conf.write_text('FLOW_REVIEW_GUARD="yes"\n')
    with pytest.raises(AtlError, match="not off or on: nothing moved"):
        client(FakeTransport(), "malek").jira_flow("DFP-1", "submit", no_github, no_github)


# --- reading Architecture review as basita-reader -----------------------------------------------------------------


def test_the_check_run_name_from_a_reusable_workflow():
    assert is_architecture_review("Architecture review")
    assert is_architecture_review("architecture-review / Architecture review")
    assert not is_architecture_review("rerun / Re-run Architecture review")


def fake_reader(answers):
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        assert cmd[1:3] == ["reader", "api"], cmd  # basita-reader only, never plain gh
        for prefix, out in answers.items():
            if cmd[3].startswith(prefix):
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="gh: Not Found (HTTP 404)")

    return run, calls


REPO = "basitasolutions/elbayoumy-farm-nutrition-platform"


def test_arch_review_state_reads_the_notice_line():
    line = f"arch-review mode=on class=not-required effort=- state=green head={HEAD} reviewed=-"
    run, calls = fake_reader(
        {
            f"repos/{REPO}/pulls/7": HEAD,
            f"repos/{REPO}/commits/{HEAD}/check-runs": json.dumps(
                [
                    {"id": 2, "name": "rerun / Re-run Architecture review", "status": "completed"},
                    {"id": 1, "name": "architecture-review / Architecture review", "status": "completed"},
                ]
            ),
            f"repos/{REPO}/check-runs/1/annotations": json.dumps(["The ubuntu-latest label will migrate", line]),
        }
    )
    state = arch_review_state(REPO, 7, run)
    assert state["state"] == "green" and state["class"] == "not-required" and state["checked"] == state["head"] == HEAD
    assert len(calls) == 3


@pytest.mark.parametrize(
    ("runs", "annotations", "state"),
    [
        ([], None, "missing"),
        ([{"id": 1, "name": "architecture-review / Architecture review", "status": "in_progress"}], None, "running"),
        ([{"id": 1, "name": "architecture-review / Architecture review", "status": "completed"}], [], "unreadable"),
    ],
)
def test_arch_review_state_without_a_result(runs, annotations, state):
    answers = {f"repos/{REPO}/pulls/7": HEAD, f"repos/{REPO}/commits/{HEAD}/check-runs": json.dumps(runs)}
    if annotations is not None:
        answers[f"repos/{REPO}/check-runs/1/annotations"] = json.dumps(annotations)
    run, _ = fake_reader(answers)
    assert arch_review_state(REPO, 7, run)["state"] == state


def test_arch_review_state_refuses_when_github_cant_be_read():
    run, _ = fake_reader({})
    with pytest.raises(AtlError, match="as basita-reader"):
        arch_review_state(REPO, 7, run)


def test_approves_guard_runs_even_with_the_submit_guard_off():
    transport = FakeTransport(_status("In Development"))
    approve_only = {"FLOW_REVIEW_GUARD": "off", "FLOW_APPROVE": "on"}
    with pytest.raises(AtlError, match="Architecture review is waiting"):
        client(transport, "yousef").jira_flow("DFP-1", "approve", lambda key: [pr()], review("waiting"), approve_only)
