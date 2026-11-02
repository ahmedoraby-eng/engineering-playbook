import json
import re
from pathlib import Path

import pytest

import atl_agent.__main__
from atl_agent import AtlClient
from atl_agent.client import BUG_DIRECT_STATUS, FLOW, JIRA_ACCOUNTS
from atl_agent.close import GUARDED
from atl_agent.flowchart import DECIDED_NOT_BUILT, OTHER_STEPS, PICTURE, WORK_TYPES, _node, main, render

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential
CATEGORY = {  # DFP's statuses and their categories
    "Refinement": "new",
    "Ready for Development": "new",
    "In Development": "indeterminate",
    "Ready for Testing": "indeterminate",
    "In Testing": "indeterminate",
    "Ready for Acceptance": "indeterminate",
    "Ready for Release": "indeterminate",
    "Released": "done",
    "Closed": "done",
}


class Workflow:
    """A DFP issue DFP-1 in a status, in a workflow where every status reaches every other (as Jira's does today).
    Answers the calls the real commands make, and records the status a transition leaves it in."""

    def __init__(self, status: str, issue_type: str = "Task", created_in: str = "Refinement"):
        self.status, self.type, self.created_in = status, issue_type, created_in
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        path = url.split("/cloud-1", 1)[1]
        if method == "POST" and path == "/rest/api/3/issue":
            self.status = self.created_in
            return 201, json.dumps({"key": "DFP-1"})
        if method == "POST" and path.endswith("/transitions"):
            self.status = list(CATEGORY)[int(json.loads(body)["transition"]["id"])]
            return 204, ""
        if path.endswith("/transitions"):
            moves = [
                {"id": str(i), "to": {"name": n, "statusCategory": {"key": c}}}
                for i, (n, c) in enumerate(CATEGORY.items())
            ]
            return 200, json.dumps({"transitions": moves})
        if "fixVersions" in path and "DFP-9" in path:
            return 200, json.dumps({"fields": {"fixVersions": [{"id": "5", "released": False}]}})
        if method == "GET" and "/rest/api/3/issue/" in path:
            fields = {
                "status": {"name": self.status, "statusCategory": {"key": CATEGORY[self.status]}},
                "issuetype": {"name": self.type},
                "labels": [],
                "fixVersions": [{"id": "5", "released": False}],
            }
            return 200, json.dumps({"fields": fields})
        return 204, ""  # assignee, labels


def run(action: str, status: str) -> str:
    """The real command behind an OTHER_STEPS row, started in `status`; returns the status it leaves the issue in."""
    flow = Workflow(status)
    salma, adam, malek = (AtlClient(a, "cloud-1", TOKEN, transport=flow) for a in ("salma", "adam", "malek"))
    if action == "ready":
        adam.jira_ready("DFP-1", "malek")
    elif action == "return":
        adam.jira_return("DFP-1")
    elif action == "bug to engineer":
        salma.jira_create("DFP", "Bug", "b", "d", assignee="malek", link="DFP-9")
    elif action == "start (work)":
        malek.jira_start("DFP-1")
    elif action == "start (testing)":
        salma.jira_start("DFP-1")
    elif action == "dismiss":
        salma.jira_dismiss("DFP-1", "Duplicate")
    else:
        pytest.fail(f"OTHER_STEPS has a row '{action}' with no test that runs its command")
    return flow.status


@pytest.mark.parametrize("action", [a for a, s in OTHER_STEPS.items() if s["command"] != "the merge (close.py)"])
def test_each_other_step_runs_from_and_to_the_statuses_the_picture_draws(action):
    step = OTHER_STEPS[action]
    if action == "bug to engineer":  # a new issue starts in Refinement
        assert run(action, "Refinement") == step["to"]
    else:
        assert run(action, step["from"]) == step["to"]


def test_the_merge_step_is_the_guard_close_py_applies():
    merged = OTHER_STEPS["merged"]
    assert (merged["from"], merged["to"]) == GUARDED["DFP"]
    assert OTHER_STEPS["bug to engineer"]["to"] == BUG_DIRECT_STATUS


def test_the_committed_picture_is_the_one_the_table_draws():
    # Fails when FLOW or OTHER_STEPS changed and `python -m atl_agent.flowchart --write` was not run.
    assert PICTURE.read_text() == render()


def test_the_picture_has_every_step_and_every_status_once():
    picture = render()
    for action, step in {**FLOW, **OTHER_STEPS}.items():
        assert f"| {action} |" in picture
        assert f"{_node(step['from'])} " in picture and f"| {_node(step['to'])}\n" in picture
    nodes = re.findall(r'^    (\w+)\["', picture, flags=re.MULTILINE)
    assert len(nodes) == len(set(nodes))
    assert "whoever last worked it" in picture and "Ahmed" in picture
    assert "712020:" not in picture  # names, never account ids


def test_the_picture_says_what_it_shows_and_draws_nothing_that_is_not_built():
    picture = render()
    head = picture.split("```mermaid")[0]
    assert head.startswith("# DFP flow (Dairy Farm Platform only)")
    assert "shows what `atl` allows" in head and "does not enforce this flow yet" in head
    assert "any status reach any status" in head
    assert f"Decided, not built (BASITA-41), so not drawn: {', '.join(DECIDED_NOT_BUILT)}." in picture
    diagram = picture.split("```mermaid")[1].split("```")[0]
    for name in ("In Acceptance", "refuse", "blocked", "restart", "reset"):
        assert name not in diagram


def test_the_other_commands_named_in_the_picture_exist():
    cli = Path(atl_agent.__main__.__file__).read_text()
    for command in {s["command"] for s in OTHER_STEPS.values()} - {"the merge (close.py)", "jira-create"}:
        assert f'"{command}"' in cli


def test_the_picture_prints_without_writing_a_file(capsys):
    assert main([]) == 0
    assert capsys.readouterr().out.strip() == render().strip()


class Jira:
    """Answers the check's reads: per (status, type) the moves offered as (target, is_global), or no issue."""

    def __init__(self, cells: dict[tuple[str, str], list[tuple[str, bool]]]):
        self.cells, self.calls = cells, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if "/search/jql" in url:
            jql = re.search(r"jql=([^&]*)", url).group(1).replace("%20", " ").replace("%22", '"').replace("%3D", "=")
            status, kind = re.search(r'status = "(.*?)" AND issuetype = "(.*?)"', jql).groups()
            key = f"{status}|{kind}"
            if (status, kind) not in self.cells:
                return 200, json.dumps({"issues": []})
            fields = {"summary": "s", "status": {"name": status}, "issuetype": {"name": kind}}
            return 200, json.dumps({"issues": [{"key": key, "fields": fields}]})
        key = url.split("/issue/")[1].split("/transitions")[0].replace("%7C", "|")
        status, kind = key.split("|")
        moves = [{"id": "1", "to": {"name": to}, "isGlobal": g} for to, g in self.cells[(status, kind)]]
        return 200, json.dumps({"transitions": moves})


def table_moves(status: str) -> list[tuple[str, bool]]:
    from atl_agent.flowchart import _steps

    return [(s["to"], False) for s in _steps() if s["from"] == status]


def run_check(cells, capsys):
    jira = Jira(cells)
    code = main(["check"], token_source=lambda agent, product: TOKEN, transport=jira)
    return code, capsys.readouterr().out, jira


def test_check_reads_only_and_reports_global_moves_as_any_move_without_failing(capsys):
    every = [(s, True) for s in CATEGORY]
    code, out, jira = run_check({("Refinement", "Task"): every, ("Ready for Release", "Bug"): every}, capsys)
    assert code == 0
    assert {method for method, _ in jira.calls} == {"GET"}
    assert (
        "allows any move: Refinement / Task on Refinement|Task: 8 moves, all global; the table's 2 moves exist" in out
    )
    assert "allows any move: Ready for Release / Bug" in out
    assert "not checked: In Testing / Story (no DFP issue of that type in it)" in out
    assert "2 any move" in out and "0 differs" in out and "is not a pass" in out
    assert "GET /transitions answers for the calling user" in out


def test_check_names_every_status_and_work_type_it_could_not_ask(capsys):
    _, out, _ = run_check({}, capsys)
    assert out.count("not checked:") == 7 * len(WORK_TYPES)
    assert "ok:" not in out and "allows any move" not in out


def test_check_after_narrowing_passes_when_jira_offers_exactly_the_table(capsys):
    cells = {("In Testing", "Task"): table_moves("In Testing")}
    code, out, _ = run_check(cells, capsys)
    assert code == 0
    assert "ok: In Testing / Task on In Testing|Task: exactly the table's 3 moves" in out
    assert "1 exact" in out


def test_check_after_narrowing_fails_on_a_move_jira_lacks_or_adds(capsys):
    short = [m for m in table_moves("In Testing") if m[0] != "Closed"]
    code, out, _ = run_check({("In Testing", "Task"): short + [("Released", False)]}, capsys)
    assert code == 1
    assert "DIFFERS: In Testing / Task on In Testing|Task: missing Closed; not in the table Released" in out
    assert "Jira differs from the table" in out


def test_check_fails_when_a_global_status_lacks_a_move_of_the_table(capsys):
    code, out, _ = run_check({("Refinement", "Task"): [("Ready for Development", True)]}, capsys)
    assert code == 1
    assert "MISSING Ready for Testing" in out


def test_check_reports_each_work_type_on_its_own(capsys):
    cells = {
        ("In Testing", "Task"): table_moves("In Testing"),
        ("In Testing", "Bug"): [("Closed", True), ("Released", True)],
    }
    code, out, _ = run_check(cells, capsys)
    assert code == 1
    assert "ok: In Testing / Task" in out
    assert "MISSING" in out and "In Testing / Bug" in out


def test_jira_transitions_gives_the_target_and_whether_it_is_global():
    jira = Jira({("Refinement", "Task"): [("Closed", True), ("Ready for Testing", False)]})
    client = AtlClient("malek", "cloud-1", TOKEN, transport=jira)
    assert client.jira_transitions("Refinement|Task") == [
        {"to": "Closed", "global": True},
        {"to": "Ready for Testing", "global": False},
    ]
    assert JIRA_ACCOUNTS  # accounts stay out of the picture, see test_the_picture_has_every_step_and_every_status_once
