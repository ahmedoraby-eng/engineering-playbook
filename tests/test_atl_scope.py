"""BASITA-245: the scope gate (Yousef's ADR, Ahmed: G1 and G2). Fakes only: no Jira, no GitHub.

The fixture is the case of 16/11/2026: the architect's review of PR #314 / DFP-269 asked for a ReportsPage fix that
already had its own open ticket, DFP-273 (linked to DFP-269 since 14/11), and the fix was done inside #314.
"""

import contextlib
import io
import json

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main
from atl_agent.scope import ScopeError, brief_lines, check_findings, load_findings, parse_acs, scope_from_fields

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"


def _p(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _status(name, category):
    return {"name": name, "statusCategory": {"key": category}}


def _issue(key, summary, status, category):
    return {"key": key, "fields": {"summary": summary, "status": _status(status, category)}}


# DFP-269 as Adam decided it (#12095): the count screen, AC1-AC6, written as lines of the description.
ACS_269 = [
    "AC1 An uncounted line shows the book at the start of today's business date.",
    "AC2 The figure does not change when the counter types; variance = physical - book.",
    "AC3 A counted line keeps its own day's book.",
    "AC4 It follows the business date on reload only.",
    "AC5 A one-day count and posting are unchanged.",
    "AC6 (optional) A tooltip says when the book is read.",
]
# ... and the bug text Salma filed, which names DFP-239's AC1 and AC8 inside sentences (not DFP-269's own).
BUG_TEXT_269 = [
    "On a multi-day count, an uncounted line shows the book of the day the count was OPENED (AC8).",
    "4. Type a physical quantity: the book becomes 35,665.000 (as AC8 and Maya #12000 require).",
    "Severity: Low. Relates to DFP-239 AC1/AC8.",
]


def fields_269(acs=True, link_273="Refinement", link_273_category="new"):
    lines = BUG_TEXT_269 + (ACS_269 if acs else [])
    return {
        "status": _status("In Testing", "indeterminate"),
        "description": {"type": "doc", "content": [_p(t) for t in lines]},
        "issuelinks": [
            {
                "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                "outwardIssue": _issue("DFP-239", "Stocktake: multi-day count", "Ready for Development", "new"),
            },
            {
                "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                "inwardIssue": _issue(
                    "DFP-273",
                    "Count report «فروق الجرد» shows the opening day's book",
                    link_273,
                    link_273_category,
                ),
            },
            {
                "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                "outwardIssue": _issue("DFP-200", "An old, finished ticket", "Released", "done"),
            },
        ],
        "parent": _issue("DFP-150", "Stocktake epic", "In Progress", "indeterminate"),
        "subtasks": [_issue("DFP-270", "QA evidence", "To Do", "new"), _issue("DFP-271", "Done sub", "Done", "done")],
    }


class Jira:
    """A fake Jira, routed by method and path: the issue's fields, transitions, assignee, comments."""

    def __init__(self, fields, status="In Testing", moves=("Ready for Development",)):
        self.fields = fields
        self.status = status
        self.moves = moves
        self.calls = []

    def __call__(self, method, url, headers, body):
        data = json.loads(body) if body else None
        self.calls.append((method, url, data))
        path = url.split("/ex/jira/" + CLOUD, 1)[-1]
        if method == "GET" and "/transitions" in path:
            moves = [{"id": str(n), "to": {"name": name}} for n, name in enumerate(self.moves, 1)]
            return 200, json.dumps({"transitions": moves})
        if method == "GET" and "fields=status,description,issuelinks,parent,subtasks" in path:
            return 200, json.dumps({"fields": self.fields})
        if method == "GET" and path.endswith("?fields=status"):
            return 200, json.dumps({"fields": {"status": _status(self.status, "indeterminate")}})
        if method == "GET" and "/changelog" in path:  # the last engineer, for fail/reject
            history = {
                "values": [
                    {
                        "author": {"accountId": "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"},
                        "created": "2026-11-14T10:00:00.000+0200",
                        "items": [{"field": "assignee", "to": "712020:e4253d3d-fed6-43d0-bbcb-acaf9b299bc3"}],
                    }
                ],
                "isLast": True,
            }
            return 200, json.dumps(history)
        if method == "GET":
            return 200, json.dumps({"fields": {"status": _status(self.status, "indeterminate")}})
        if method == "POST" and path.endswith("/comment"):
            return 201, json.dumps({"id": "1", "author": {"displayName": "x"}})
        return 204, ""

    def writes(self):
        return [(m, u.split("/rest/api/3/", 1)[-1]) for m, u, _ in self.calls if m != "GET"]

    def comments(self):
        return {
            u.split("/issue/", 1)[1].split("/", 1)[0]: json.dumps(d)
            for m, u, d in self.calls
            if m == "POST" and u.endswith("/comment")
        }


def client(jira, agent):
    return AtlClient(agent, CLOUD, TOKEN, transport=jira)


def write_findings(tmp_path, findings):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(findings), encoding="utf-8")
    return str(path)


REPORTS_FINDING = {"file": "src/pages/ReportsPage.tsx:584", "what": "the count report shows the opening day's book"}


# --- 1. parse_acs and issue_scope (G1 data) ------------------------------------------------------------------------
def test_parse_acs_reads_definitions_at_line_start_only():
    text = "\n".join(BUG_TEXT_269 + ACS_269 + ["- AC7 in a list", "1. AC8 numbered", "**AC9** bold", "BAC10", "AC11x"])
    assert parse_acs(text) == ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "AC7", "AC8", "AC9"]
    assert parse_acs("\n".join(BUG_TEXT_269)) == []  # DFP-239's AC1/AC8 named in sentences are not DFP-269's
    assert parse_acs(None) == [] and parse_acs("") == []


def test_issue_scope_from_jira_fields():
    jira = Jira(fields_269())
    scope = client(jira, "yousef").issue_scope("DFP-269")
    assert scope["acs"] == ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6"]
    assert [x["key"] for x in scope["open_links"]] == ["DFP-239", "DFP-273"]
    assert [x["key"] for x in scope["closed_links"]] == ["DFP-200"]  # by status category, not by name
    assert scope["open_links"][1]["relation"] == "relates to" and scope["open_links"][1]["status"] == "Refinement"
    assert scope["parent"]["key"] == "DFP-150"
    assert [x["key"] for x in scope["open_subtasks"]] == ["DFP-270"]
    assert jira.writes() == []  # reads only


def test_issue_scope_refuses_a_bad_key():
    with pytest.raises(AtlError, match="not an issue key"):
        client(Jira({}), "yousef").issue_scope("dfp-269")


def test_scope_with_no_acs_and_no_links():
    scope = scope_from_fields("DFP-1", {"issuelinks": [], "subtasks": []}, "no criteria here")
    assert scope == {
        "key": "DFP-1",
        "acs": [],
        "open_links": [],
        "closed_links": [],
        "parent": None,
        "open_subtasks": [],
    }
    lines = "\n".join(brief_lines(scope, "salma"))
    assert "has no numbered acceptance criteria" in lines and "none (no open linked issue)" in lines


def test_brief_lines_are_the_same_every_run_and_name_the_ticket_to_use():
    scope = scope_from_fields("DFP-269", fields_269(), "\n".join(ACS_269))
    once, twice = brief_lines(scope, "malek"), brief_lines(scope, "malek")
    assert once == twice
    text = "\n".join(once)
    assert "In scope: DFP-269's acceptance criteria AC1, AC2, AC3, AC4, AC5, AC6" in text
    assert "DFP-273 (relates to) «Count report «فروق الجرد» shows the opening day's book», Refinement" in text
    assert "atl malek jira-comment <that key>" in text and "not into DFP-269's branch" in text
    assert "DFP-200" not in text  # a closed link is not offered as a home for a finding


# --- 2. findings files ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data, why",
    [
        ("not json", "not readable JSON"),
        ("[]", "non-empty JSON list"),
        ('{"ac": "AC1"}', "non-empty JSON list"),
        ('["x"]', "an object"),
        ('[{"ac": "AC1"}]', "«what»"),
        ('[{"ac": "AC1", "what": " "}]', "«what»"),
        ('[{"what": "x"}]', "exactly one of"),
        ('[{"ac": "AC1", "out_of_scope": "DFP-273", "what": "x"}]', "exactly one of"),
        ('[{"ac": "AC1", "what": "x", "severity": "high"}]', "unknown field"),
        ('[{"ac": 1, "what": "x"}]', "non-empty string"),
    ],
)
def test_load_findings_refuses_bad_shapes(data, why):
    with pytest.raises(ScopeError, match=why):
        load_findings("f.json", data)


def test_load_findings_reads_a_file(tmp_path):
    path = write_findings(tmp_path, [{"ac": "AC2", "what": " w "}])
    assert load_findings(path) == [{"ac": "AC2", "what": "w"}]


def test_check_findings_splits_in_and_out_of_scope():
    scope = scope_from_fields("DFP-269", fields_269(), "\n".join(ACS_269))
    inside, outside = check_findings(
        scope, [{"ac": "ac 2", "what": "x"}, {"out_of_scope": "dfp-273", "what": "y", "file": "a.tsx"}]
    )
    assert inside == [{"ac": "AC2", "what": "x"}]
    assert outside == [{"out_of_scope": "DFP-273", "what": "y", "file": "a.tsx"}]


@pytest.mark.parametrize(
    "finding, why",
    [
        ({"ac": "AC7", "what": "x"}, "AC7 is not one of DFP-269's acceptance criteria"),
        ({"ac": "AC 2 and 3", "what": "x"}, "is not an AC number"),
        ({"out_of_scope": "DFP-999", "what": "x"}, "DFP-999 is not an open issue linked to DFP-269"),
        ({"out_of_scope": "DFP-200", "what": "x"}, "DFP-200 is linked to DFP-269 but closed"),
        ({"out_of_scope": "DFP-269", "what": "x"}, "must name another issue"),
    ],
)
def test_check_findings_refuses_what_has_no_home(finding, why):
    scope = scope_from_fields("DFP-269", fields_269(), "\n".join(ACS_269))
    with pytest.raises(ScopeError, match=why):
        check_findings(scope, [{"ac": "AC1", "what": "fine"}, finding])


def test_no_acs_means_nothing_goes_back_as_changes_needed():
    scope = scope_from_fields("DFP-269", fields_269(acs=False), "\n".join(BUG_TEXT_269))
    with pytest.raises(ScopeError, match="has no numbered acceptance criteria"):
        check_findings(scope, [{"ac": "AC1", "what": "x"}])  # DFP-239's AC1 in the bug text is not DFP-269's
    assert check_findings(scope, [{"out_of_scope": "DFP-273", "what": "x"}])[1][0]["out_of_scope"] == "DFP-273"


# --- 3. send_back (G2): today's case ---------------------------------------------------------------------------------
def test_todays_case_the_reports_finding_goes_to_dfp_273_and_pr_314_stays():
    jira = Jira(fields_269(), status="In Development")
    result = client(jira, "yousef").send_back("DFP-269", "changes", [{"out_of_scope": "DFP-273", **REPORTS_FINDING}])
    assert result["status"] is None and result["in_scope"] == [] and result["review_text"] == ""
    assert jira.writes() == [("POST", "issue/DFP-273/comment"), ("POST", "issue/DFP-269/comment")]
    comments = jira.comments()
    assert "ReportsPage.tsx:584" in comments["DFP-273"] and "not sent to DFP-269's developer" in comments["DFP-273"]
    assert "no finding is within DFP-269's acceptance criteria" in comments["DFP-269"]
    assert "posted on their own tickets: DFP-273" in comments["DFP-269"]


def test_todays_case_cited_as_an_ac_is_refused_before_any_write():
    jira = Jira(fields_269(), status="In Development")
    with pytest.raises(AtlError, match="refused, nothing written: finding 1: AC7 is not one of DFP-269's"):
        client(jira, "yousef").send_back("DFP-269", "changes", [{"ac": "AC7", **REPORTS_FINDING}])
    assert jira.writes() == []


def test_changes_with_an_in_scope_finding_gives_the_review_text():
    jira = Jira(fields_269(), status="In Development")
    findings = [
        {"ac": "AC2", "file": "src/stocktake/Line.tsx:40", "what": "the figure moves while typing"},
        {"out_of_scope": "DFP-273", **REPORTS_FINDING},
    ]
    result = client(jira, "yousef").send_back("DFP-269", "changes", findings)
    assert result["status"] is None  # the review moves nothing in Jira
    assert result["review_text"].startswith("Changes needed, each against DFP-269's acceptance criteria:")
    assert "- AC2 (src/stocktake/Line.tsx:40): the figure moves while typing" in result["review_text"]
    assert "Out of scope (their own tickets, not for this PR): DFP-273." in result["review_text"]
    assert "ReportsPage" not in result["review_text"]
    assert [w for w in jira.writes() if "transitions" in w[1] or "assignee" in w[1]] == []


def test_fail_moves_only_with_an_in_scope_finding():
    jira = Jira(fields_269(), status="In Testing")
    result = client(jira, "salma").send_back(
        "DFP-269", "fail", [{"ac": "AC3", "what": "a counted line lost its day's book"}], why="QA-14 red"
    )
    assert result["status"] == "Ready for Development"
    writes = jira.writes()
    assert ("POST", "issue/DFP-269/transitions") in writes and ("PUT", "issue/DFP-269/assignee") in writes
    assert "Test failed, each against DFP-269's acceptance criteria" in jira.comments()["DFP-269"]
    assert "QA-14 red" in jira.comments()["DFP-269"]


def test_fail_with_only_out_of_scope_findings_does_not_move():
    jira = Jira(fields_269(), status="In Testing")
    result = client(jira, "salma").send_back("DFP-269", "fail", [{"out_of_scope": "DFP-273", **REPORTS_FINDING}])
    assert result["status"] is None
    assert [w for w in jira.writes() if "transitions" in w[1] or "assignee" in w[1]] == []


@pytest.mark.parametrize(
    "agent, action", [("malek", "fail"), ("salma", "reject"), ("adam", "changes"), ("yousef", "fail")]
)
def test_send_back_is_only_for_its_owner(agent, action):
    jira = Jira(fields_269())
    with pytest.raises(AtlError, match=f"{action} is for"):
        client(jira, agent).send_back("DFP-269", action, [{"ac": "AC1", "what": "x"}])
    assert jira.calls == []  # refused before even reading


# --- 4. the command line -------------------------------------------------------------------------------------------
def _run(argv, jira):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([*argv, "--cloud-id", CLOUD], token_source=lambda *a: TOKEN, transport=jira)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("action", ["fail", "reject"])
def test_cli_fail_and_reject_need_a_findings_file(action):
    agent = "salma" if action == "fail" else "adam"
    jira = Jira(fields_269())
    code, _, err = _run([agent, "jira-flow", "DFP-269", action, "free text only"], jira)
    assert code == 2 and "takes --findings <file> (BASITA-245)" in err and jira.calls == []


def test_cli_findings_only_where_they_belong(tmp_path):
    path = write_findings(tmp_path, [{"ac": "AC1", "what": "x"}])
    code, _, err = _run(["salma", "jira-flow", "DFP-269", "pass", "ok", "--findings", path], Jira({}))
    assert code == 2 and "--findings is for jira-flow fail|reject and jira-changes" in err


def test_cli_jira_changes_todays_case(tmp_path):
    jira = Jira(fields_269(), status="In Development")
    path = write_findings(tmp_path, [{"out_of_scope": "DFP-273", **REPORTS_FINDING}])
    code, out, err = _run(["yousef", "jira-changes", "DFP-269", "--findings", path], jira)
    assert code == 0, err
    assert "DFP-269 not moved, changes: 0 finding(s) in scope, 1 out of scope (posted on DFP-273)" in out
    assert "PR review text" not in out


def test_cli_jira_changes_refusal_writes_nothing(tmp_path):
    jira = Jira(fields_269(), status="In Development")
    path = write_findings(tmp_path, [{"ac": "AC7", **REPORTS_FINDING}])
    code, _, err = _run(["yousef", "jira-changes", "DFP-269", "--findings", path], jira)
    assert code == 1 and "refused, nothing written" in err and jira.writes() == []


def test_cli_jira_scope_prints_the_brief_block_and_json():
    code, out, _ = _run(["malek", "jira-scope", "DFP-269"], Jira(fields_269()))
    assert code == 0 and out.startswith("Scope (BASITA-245") and "DFP-273 (relates to)" in out
    code, out, _ = _run(["malek", "jira-scope", "DFP-269", "--json"], Jira(fields_269()))
    assert code == 0 and json.loads(out)["acs"] == ["AC1", "AC2", "AC3", "AC4", "AC5", "AC6"]
