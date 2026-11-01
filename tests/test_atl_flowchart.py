import json
import re
from pathlib import Path

import atl_agent.__main__
from atl_agent import AtlClient
from atl_agent.client import FLOW
from atl_agent.flowchart import OTHER_STEPS, PICTURE, _node, main, render

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential


class Jira:
    """A Jira that answers GET only: one DFP issue per status, with the given moves. Records every call."""

    def __init__(self, statuses: dict[str, list[tuple[str, bool]]]):
        self.statuses = statuses
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url))
        if "/search/jql" in url:
            status = re.search(r"status%20%3D%20%22(.*?)%22", url).group(1).replace("%20", " ")
            if status not in self.statuses:
                return 200, json.dumps({"issues": []})
            fields = {"summary": "s", "status": {"name": status}, "issuetype": {"name": "Task"}}
            return 200, json.dumps({"issues": [{"key": f"DFP-{len(status)}", "fields": fields}]})
        status = next(s for s in self.statuses if f"DFP-{len(s)}/transitions" in url)
        moves = [
            {"id": str(i), "to": {"name": to}, "isGlobal": is_global}
            for i, (to, is_global) in enumerate(self.statuses[status])
        ]
        return 200, json.dumps({"transitions": moves})


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
    assert "the last engineer" in picture and "Ahmed" in picture  # the next owner is drawn, not an account id
    assert "712020:" not in picture


def test_the_steps_of_other_commands_name_commands_that_exist():
    cli = (Path(atl_agent.__main__.__file__)).read_text()
    for command in {s["command"] for s in OTHER_STEPS.values()} - {"the merge (close.py)"}:
        assert f'"{command}"' in cli


def test_the_picture_prints_without_writing_a_file(capsys):
    assert main([]) == 0
    assert capsys.readouterr().out.strip() == render().strip()


def test_check_reads_only_and_reports_global_moves_without_failing(capsys):
    moves = [("Refinement", True), ("Ready for Development", True), ("Ready for Testing", True), ("Closed", True)]
    jira = Jira({"Refinement": moves, "Ready for Release": [("Released", True), ("Closed", True)]})
    code = main(["check"], token_source=lambda agent, product: TOKEN, transport=jira)
    out = capsys.readouterr().out
    assert code == 0
    assert {method for method, _ in jira.calls} == {"GET"}
    assert "ok: Refinement -> Ready for Development (ready)" in out
    assert "ok: Ready for Release -> Released (merged)" in out
    assert "not checked: In Testing (no DFP issue in it to ask)" in out
    assert "Jira allows 3 moves, 3 of them global; 1 are not in the table (Closed)" in out  # not the move to itself
    assert "Statuses checked: 2. 2 of them offer only global moves" in out


def test_check_fails_when_a_move_of_the_table_does_not_exist(capsys):
    jira = Jira({"Refinement": [("Ready for Development", False)]})
    code = main(["check"], token_source=lambda agent, product: TOKEN, transport=jira)
    out = capsys.readouterr().out
    assert code == 1
    assert "MISSING: Refinement -> Ready for Testing (return)" in out
    assert "ok: Refinement -> Ready for Development (ready)" in out
    assert "Statuses checked: 1. 0 of them offer only global moves" in out


def test_jira_transitions_gives_the_target_and_whether_it_is_global():
    jira = Jira({"Refinement": [("Closed", True), ("Ready for Testing", False)]})
    client = AtlClient("malek", "cloud-1", TOKEN, transport=jira)
    assert client.jira_transitions("DFP-10") == [
        {"to": "Closed", "global": True},
        {"to": "Ready for Testing", "global": False},
    ]
