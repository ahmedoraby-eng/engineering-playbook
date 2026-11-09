"""atl_agent.release (BASITA-208): DFP Ready for Release -> Released when every PR that names it is merged, offline."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.client import AGENT_GH, PR_LIST_LIMIT, PR_REPOS
from atl_agent.release import (
    JQL,
    LOG_HEADING,
    RepoPrs,
    append_log,
    decide,
    evidence,
    main,
    names_key,
    release_issues,
)

TOKEN = "tok-" + "r" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"
RFR = "Ready for Release"
CURRENT = frozenset({"0.2.1", "0.2.1.2"})


class FakeJira:
    """Jira, routed by URL. `issues` maps key -> [status, type]; `listed` is what the Ready for Release search returns
    (default: every issue in Ready for Release that is not an Epic, as the JQL asks). `after_list` changes statuses
    between the search and the re-read, as someone moving an issue meanwhile would."""

    def __init__(self, issues, listed=None, after_list=None, fail=()):
        self.issues = {k: list(v) for k, v in issues.items()}
        self.listed = listed
        self.after_list = after_list or {}
        self.fail = set(fail)
        self.calls = []

    @staticmethod
    def _row(key, status, kind, fixes=("0.2.1",)):
        fields = {"summary": "s", "status": {"name": status}, "issuetype": {"name": kind}, "labels": []}
        fields["fixVersions"] = [{"id": str(i), "name": name} for i, name in enumerate(fixes)]
        return {"key": key, "fields": fields}

    def __call__(self, method, url, headers, body):
        path = url.split(CLOUD, 1)[1]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        parsed = urlparse(path)
        if (method, parsed.path) in self.fail or (method, "*") in self.fail:
            return 500, "boom"
        if method == "GET" and parsed.path == "/rest/api/3/search/jql":
            jql = parse_qs(parsed.query)["jql"][0]
            if jql.startswith("key = "):
                key = jql.split(" = ", 1)[1]
                found = [self._row(key, *self.issues[key])] if key in self.issues else []
                return 200, json.dumps({"issues": found})
            assert jql == JQL
            keys = self.listed if self.listed is not None else [
                k for k, (s, t, *_) in self.issues.items() if s == RFR and t != "Epic"
            ]  # fmt: skip
            rows = [self._row(k, *self.issues[k]) for k in keys]
            for key, status in self.after_list.items():
                self.issues[key][0] = status
            return 200, json.dumps({"issues": rows})
        key = parsed.path.split("/")[5]
        if method == "GET" and parsed.path.endswith("/transitions"):
            moves = [
                {"id": "41", "to": {"name": "Closed", "statusCategory": {"key": "done"}}},
                {"id": "31", "to": {"name": "Released", "statusCategory": {"key": "done"}}},
                {"id": "7", "to": {"name": "In Testing", "statusCategory": {"key": "indeterminate"}}},
            ]
            return 200, json.dumps({"transitions": moves})
        if method == "POST" and parsed.path.endswith("/transitions"):
            assert payload == {"transition": {"id": "31"}}
            self.issues[key][0] = "Released"
            return 204, ""
        if method == "POST" and parsed.path.endswith("/comment"):
            return 201, json.dumps({"id": "9"})
        raise AssertionError(f"unexpected call {method} {path}")

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]

    def moved(self):
        return [p.split("/")[5] for m, p, _ in self.calls if m == "POST" and p.endswith("/transitions")]

    def comments(self):
        texts = []
        for m, p, payload in self.calls:
            if m == "POST" and p.endswith("/comment"):
                paragraphs = payload["body"]["content"]
                text = "\n".join("".join(n.get("text", "") for n in para.get("content", [])) for para in paragraphs)
                texts.append((p.split("/")[5], text))
        return texts


def client(jira):
    return AtlClient("sherif", CLOUD, TOKEN, transport=jira)


def pr(number, state, title="x", branch="b", body="", repo="basitasolutions/elbayoumy-farm-nutrition-platform"):
    return {
        "repo": repo,
        "number": number,
        "state": state,
        "title": title,
        "headRefName": branch,
        "body": body,
        "url": f"https://github.com/{repo}/pull/{number}",
    }


def source(*prs):
    return lambda key: list(prs)


# ---------- the whole-word match ----------


@pytest.mark.parametrize(
    "fields, named",
    [
        ({"title": "DFP-15: fix the header"}, True),
        ({"title": "DFP-153: other work"}, False),
        ({"title": "DFP-153, DFP-154: checklist"}, False),
        ({"title": "XDFP-15 thing"}, False),
        ({"title": "DFP-15a"}, False),
        ({"headRefName": "malek/DFP-15-short-name"}, True),
        ({"headRefName": "feat/dfp-15-lowercase"}, True),
        ({"headRefName": "malek/DFP-150-other"}, False),
        ({"headRefName": "malek/DFP-15"}, True),
        ({"headRefName": "malek/DFP 15-short"}, False),
        ({"title": "Builds on DFP 15", "body": "Refs DFP 15"}, False),
        ({"title": "DFP 15: spaced"}, False),
        # description: only a Refs / Closes line counts (BASITA-228)
        ({"body": "Refs DFP-15"}, True),
        ({"body": "Closes DFP-15"}, True),
        ({"body": "## Summary\nDid things.\n\nRefs DFP-15\n\n## Tests\nran them"}, True),
        ({"body": "closes dfp-15"}, True),
        ({"body": "REFS: DFP-15"}, True),
        ({"body": "  Closes DFP-15"}, True),
        ({"body": "- Refs DFP-15"}, True),
        ({"body": "Refs DFP-16, DFP-15"}, True),
        ({"body": "Refs DFP-16 and DFP-15"}, True),
        ({"body": "Builds on DFP-15, see there."}, False),
        ({"body": "DFP-195 / DFP-15 do not conflict"}, False),
        ({"body": "Related to DFP-15"}, False),
        ({"body": "This PR refs DFP-15 in passing"}, False),
        ({"body": "Refs DFP 15"}, False),
        ({"body": "Builds on DFP 15, see there."}, False),
        ({"body": "Refs DFP-153"}, False),
        ({"body": "Closes DFP-151"}, False),
        ({"body": "Refs DFP-15a"}, False),
        ({"body": "Refs XDFP-15"}, False),
        ({"body": "Refs DFP-16\nand also DFP-15 is mentioned here"}, False),
        ({"body": "<!-- Closes DFP-15 -->\nCloses DFP-16"}, False),
        ({"body": "<!--\nRefs DFP-15\n-->"}, False),
        ({}, False),
        ({"title": None, "headRefName": None, "body": None}, False),
    ],
)
def test_names_key_counts_title_branch_and_refs_closes_lines_only(fields, named):
    assert names_key(fields, "DFP-15") is named


# ---------- the decision ----------


def test_decide_every_branch():
    assert decide([]) == ("release", "")
    assert decide([pr(1, "MERGED")]) == ("release", "")
    assert decide([pr(1, "MERGED"), pr(2, "CLOSED")]) == ("release", "")
    assert decide([pr(1, "MERGED"), pr(2, "OPEN")]) == ("leave", "open PR elbayoumy-farm-nutrition-platform#2")
    verdict, reason = decide([pr(2, "CLOSED"), pr(3, "CLOSED")])
    assert verdict == "FLAG" and "closed without merging" in reason and "#2, " in reason
    verdict, reason = decide([pr(1, "MERGED"), pr(4, "DRAFT")])
    assert verdict == "leave" and "unknown state" in reason


def test_merged_prs_move_it_and_the_comment_lists_them():
    jira = FakeJira({"DFP-153": [RFR, "Task"]})
    prs = source(pr(207, "MERGED", title="DFP-153, DFP-154: checklist", body="Refs DFP-153"), pr(9, "MERGED"))
    lines, moved, failed = release_issues(client(jira), prs, versions=CURRENT)
    assert lines == ["DFP-153: Ready for Release -> Released (elbayoumy-farm-nutrition-platform#207)"]
    assert jira.moved() == ["DFP-153"] and not failed
    assert moved and "| DFP-153 | Ready for Release -> Released |" in moved[0] and "/pull/207)" in moved[0]
    [(key, text)] = jira.comments()
    assert key == "DFP-153"
    assert text.startswith(
        "Released automatically (BASITA-208): accepted (Ready for Release) and every PR that names it is merged:"
    )
    assert "https://github.com/basitasolutions/elbayoumy-farm-nutrition-platform/pull/207" in text
    assert "/pull/9" not in text  # PR 9 doesn't name the key
    assert text.rstrip().endswith("— Sherif (Basita AI)")
    # the move comes before the comment
    writes = jira.writes()
    assert writes[0][1].endswith("/transitions") and writes[1][1].endswith("/comment")


def test_no_pr_moves_it_as_design_or_qa_work():
    jira = FakeJira({"DFP-116": [RFR, "Story"]})
    lines, moved, _ = release_issues(
        client(jira), source(pr(5, "MERGED", title="DFP-1160 unrelated")), versions=CURRENT
    )
    assert lines == ["DFP-116: Ready for Release -> Released (no PR)"]
    assert moved[0].endswith("| no PR |")
    [(_, text)] = jira.comments()
    assert "accepted (Ready for Release) and no PR names DFP-116 (design or QA work)." in text


def test_an_open_pr_that_only_mentions_the_key_in_prose_does_not_hold_it():
    """BASITA-228: elbayoumy#250 said "DFP-195 / DFP-192 do not conflict" and must not count as DFP-192's PR."""
    mention = pr(
        250, "OPEN", title="DFP-195: other work", branch="malek/DFP-195-x", body="DFP-195 / DFP-192 do not conflict"
    )
    spaced = pr(251, "OPEN", title="DFP-196: more", body="Refs DFP-196\nAlso touches DFP 192.")
    own = pr(252, "OPEN", title="DFP-192: its own PR")
    jira = FakeJira({"DFP-192": [RFR, "Task"]})
    lines, _, _ = release_issues(client(jira), source(mention, spaced), versions=CURRENT)
    assert lines == ["DFP-192: Ready for Release -> Released (no PR)"]
    jira = FakeJira({"DFP-192": [RFR, "Task"]})
    lines, _, _ = release_issues(client(jira), source(mention, spaced, own), versions=CURRENT)
    assert lines == ["DFP-192: left, open PR elbayoumy-farm-nutrition-platform#252"]
    assert jira.moved() == []


def test_an_open_pr_leaves_it():
    jira = FakeJira({"DFP-20": [RFR, "Task"]})
    lines, moved, failed = release_issues(
        client(jira), source(pr(1, "MERGED", "DFP-20"), pr(3, "OPEN", "DFP-20 more")), versions=CURRENT
    )
    assert lines == ["DFP-20: left, open PR elbayoumy-farm-nutrition-platform#3"]
    assert jira.writes() == [] and moved == [] and not failed


def test_only_closed_unmerged_prs_leave_it_flagged():
    jira = FakeJira({"DFP-21": [RFR, "Task"]})
    lines, _, _ = release_issues(client(jira), source(pr(4, "CLOSED", "DFP-21")), versions=CURRENT)
    assert lines[0].startswith("DFP-21: FLAG left in Ready for Release, its PRs were closed without merging")
    assert jira.writes() == []


def test_a_failed_search_leaves_it_and_counts_as_a_failure():
    jira = FakeJira({"DFP-22": [RFR, "Task"], "DFP-23": [RFR, "Task"]})

    def broken(key):
        raise AtlError("could not read the pull requests of o/r: HTTP 401")

    lines, moved, failed = release_issues(client(jira), broken, versions=CURRENT)
    assert lines == [
        "DFP-22: left, the PR search failed (could not read the pull requests of o/r: HTTP 401)",
        "DFP-23: left, the PR search failed (could not read the pull requests of o/r: HTTP 401)",
    ]
    assert failed and moved == [] and jira.writes() == []


# ---------- the guards ----------


MOVED_ON = ["Ready for Acceptance", "In Testing", "Released", "Closed", "Ready for Development"]


@pytest.mark.parametrize("status", MOVED_ON)
def test_the_re_read_guard_skips_an_issue_that_moved_meanwhile(status):
    jira = FakeJira({"DFP-30": [RFR, "Task"]}, after_list={"DFP-30": status})
    lines, moved, failed = release_issues(client(jira), source(), versions=CURRENT)
    assert lines == [f"DFP-30: left, now in {status}"]
    assert jira.writes() == [] and moved == [] and not failed
    reads = [p for m, p, _ in jira.calls if m == "GET" and "search/jql" in p]
    assert len(reads) == 2  # the list, then the issue itself again


def test_an_epic_is_never_moved_even_if_the_search_returns_it():
    jira = FakeJira({"DFP-1": [RFR, "Epic"]}, listed=["DFP-1"])
    lines, _, _ = release_issues(client(jira), source(), versions=CURRENT)
    assert lines == ["DFP-1: left, an Epic is never released automatically"]
    assert jira.writes() == []


def test_an_issue_that_became_an_epic_is_not_moved():
    jira = FakeJira({"DFP-2": [RFR, "Task"]})
    jira.after_list = {}
    real = jira.__call__

    def turning(method, url, headers, body):
        answer = real(method, url, headers, body)
        if "jql=key" in url:  # the re-read sees an Epic
            data = json.loads(answer[1])
            data["issues"][0]["fields"]["issuetype"]["name"] = "Epic"
            return answer[0], json.dumps(data)
        return answer

    lines, _, _ = release_issues(AtlClient("sherif", CLOUD, TOKEN, transport=turning), source(), versions=CURRENT)
    assert lines == ["DFP-2: left, an Epic is never released automatically"]
    assert jira.writes() == []


def test_never_from_another_status_or_another_project():
    jira = FakeJira({"DFP-3": ["In Testing", "Task"], "ELB-4": [RFR, "Task"]}, listed=["DFP-3", "ELB-4"])
    lines, _, _ = release_issues(client(jira), source(), versions=CURRENT)
    assert lines == ["DFP-3: left in In Testing, only Ready for Release moves", "ELB-4: left, not a DFP issue"]
    assert jira.writes() == []


def test_a_failed_move_writes_no_comment_and_counts_as_a_failure():
    jira = FakeJira({"DFP-5": [RFR, "Task"]}, fail={("POST", "/rest/api/3/issue/DFP-5/transitions")})
    lines, moved, failed = release_issues(client(jira), source(), versions=CURRENT)
    assert lines[0].startswith("DFP-5: left, the move failed")
    assert failed and moved == [] and jira.comments() == []


def test_a_failed_comment_after_the_move_is_reported():
    jira = FakeJira({"DFP-6": [RFR, "Task"]}, fail={("POST", "/rest/api/3/issue/DFP-6/comment")})
    lines, moved, failed = release_issues(client(jira), source(), versions=CURRENT)
    assert lines[0].startswith("DFP-6: Ready for Release -> Released (no PR), but the comment failed")
    assert failed and len(moved) == 1  # it did move, so the log records it


def test_evidence_texts():
    merged = [pr(207, "MERGED"), pr(3, "MERGED", repo="basitasolutions/basita-ops")]
    assert evidence("DFP-153", merged) == (
        "Released automatically (BASITA-208): accepted (Ready for Release) and every PR that names it is merged:\n"
        "https://github.com/basitasolutions/elbayoumy-farm-nutrition-platform/pull/207\n"
        "https://github.com/basitasolutions/basita-ops/pull/3"
    )
    assert evidence("DFP-139", []) == (
        "Released automatically (BASITA-208): accepted (Ready for Release) and no PR names DFP-139 (design or QA work)."
    )


# ---------- the PR source ----------


class Done:
    def __init__(self, returncode=0, stdout="[]", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_repo_prs_reads_each_repo_once_as_the_reader_and_keeps_whole_word_names():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        repo = command[command.index("-R") + 1]
        prs = [
            {"number": 1, "title": "DFP-15 x", "headRefName": "a", "body": "", "state": "MERGED", "url": "u1"},
            {"number": 2, "title": "DFP-153 y", "headRefName": "b", "body": "", "state": "OPEN", "url": "u2"},
        ]
        return Done(stdout=json.dumps(prs if "elbayoumy" in repo else []))

    prs = RepoPrs(PR_REPOS, run)
    assert [p["number"] for p in prs("DFP-15")] == [1]
    assert [p["number"] for p in prs("DFP-153")] == [2]
    assert prs("DFP-15")[0]["repo"] == "basitasolutions/elbayoumy-farm-nutrition-platform"
    assert len(calls) == len(PR_REPOS)  # once per repo, however many keys
    assert all(c[:4] == [AGENT_GH, "reader", "pr", "list"] and c[c.index("--state") + 1] == "all" for c in calls)
    assert not any(c[0] == "gh" for c in calls)


@pytest.mark.parametrize(
    "answer",
    [
        Done(returncode=1, stderr="agent-gh: refused: basita-reader is not set up yet"),
        Done(stdout="not json"),
        Done(stdout=json.dumps([{"number": n} for n in range(PR_LIST_LIMIT)])),
    ],
)
def test_repo_prs_fails_closed_for_every_key(answer):
    prs = RepoPrs(("o/r",), lambda command, **kwargs: answer)
    for key in ("DFP-1", "DFP-2"):
        with pytest.raises(AtlError, match="o/r"):
            prs(key)


# ---------- the CLI and the log ----------

ENV = {"ATL_CLOUD_ID": CLOUD, "ATL_TOKEN": TOKEN}


def no_keychain(*args):
    raise AssertionError("the Keychain is not read when ATL_TOKEN is set")


def test_dry_run_makes_no_post_put_or_transition_and_writes_no_log(tmp_path, capsys):
    jira = FakeJira({"DFP-153": [RFR, "Task"], "DFP-20": [RFR, "Task"]})
    log = tmp_path / "attention.md"
    prs = source(pr(207, "MERGED", "DFP-153"), pr(3, "OPEN", "DFP-20"))
    code = main(
        ["--agent", "sherif", "--versions", "0.2.1,0.2.1.2", "--dry-run", "--log", str(log)],
        ENV,
        jira,
        no_keychain,
        prs,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert all(m == "GET" for m, _, _ in jira.calls)
    assert not any(p.endswith("/transitions") for _, p, _ in jira.calls)
    assert "DFP-153: would move Ready for Release -> Released (elbayoumy-farm-nutrition-platform#207)" in out
    assert "DFP-20: left, open PR" in out and "dry run, nothing written" in out
    assert not log.exists()


def test_cli_moves_and_appends_the_log_under_its_heading(tmp_path, capsys):
    jira = FakeJira({"DFP-139": [RFR, "Task"]})
    log = tmp_path / "attention.md"
    log.write_text("# Jira attention\n\n(none)")
    assert (
        main(["--agent", "sherif", "--versions", "0.2.1,0.2.1.2", "--log", str(log)], ENV, jira, no_keychain, source())
        == 0
    )
    assert "DFP-139: Ready for Release -> Released (no PR)" in capsys.readouterr().out
    text = log.read_text()
    assert text.startswith("# Jira attention\n\n(none)\n\n" + LOG_HEADING + "\n")
    assert "| DFP-139 | Ready for Release -> Released | no PR |" in text
    jira2 = FakeJira({"DFP-140": [RFR, "Task"]})
    assert (
        main(["--agent", "sherif", "--versions", "0.2.1,0.2.1.2", "--log", str(log)], ENV, jira2, no_keychain, source())
        == 0
    )
    text = log.read_text()
    assert text.count(LOG_HEADING) == 1 and text.rstrip().endswith("| no PR |") and "DFP-140" in text


def test_cli_exits_1_when_a_read_failed_and_reads_the_keychain_without_atl_token(capsys):
    jira = FakeJira({"DFP-7": [RFR, "Task"]})
    asked = []

    def keychain(account, product):
        asked.append((account, product))
        return TOKEN

    def broken(key):
        raise AtlError("HTTP 502")

    assert (
        main(["--agent", "sherif", "--versions", "0.2.1,0.2.1.2"], {"ATL_CLOUD_ID": CLOUD}, jira, keychain, broken) == 1
    )
    assert asked == [("sherif", "jira")]
    assert "DFP-7: left, the PR search failed (HTTP 502)" in capsys.readouterr().out


def test_cli_jira_search_failure_is_an_error(capsys):
    jira = FakeJira({}, fail={("GET", "/rest/api/3/search/jql")})
    assert main(["--agent", "sherif", "--versions", "0.2.1,0.2.1.2"], ENV, jira, no_keychain, source()) == 1
    assert "atl-release: GET jira/rest/api/3/search/jql" in capsys.readouterr().err


def test_append_log_with_nothing_moved_leaves_the_file_alone(tmp_path):
    log = tmp_path / "a.md"
    append_log(str(log), [])
    assert not log.exists()


# ---------- the Fix Version guard (Ahmed, 08/11/2026): only current versions are released ----------


@pytest.mark.parametrize(
    "fixes, reason",
    [
        (["0.2.2"], "Fix Version 0.2.2 is not a current version (0.2.1, 0.2.1.2), so it is planned work"),
        (["0.2.1", "0.2.2"], "Fix Version 0.2.2 is not a current version"),
        ([], "it has no Fix Version"),
    ],
)
def test_a_planned_or_missing_fix_version_leaves_it(fixes, reason):
    jira = FakeJira({"DFP-30": [RFR, "Task", fixes]})
    lines, moved, failed = release_issues(client(jira), source(pr(1, "MERGED", "DFP-30")), versions=CURRENT)
    assert len(lines) == 1 and lines[0].startswith("DFP-30: left, ") and reason in lines[0]
    assert jira.writes() == [] and moved == [] and not failed


@pytest.mark.parametrize("fixes", [["0.2.1"], ["0.2.1.2"], ["0.2.1", "0.2.1.2"]])
def test_every_fix_version_current_moves_it(fixes):
    jira = FakeJira({"DFP-31": [RFR, "Task", fixes]})
    lines, _, _ = release_issues(client(jira), source(), versions=CURRENT)
    assert lines == ["DFP-31: Ready for Release -> Released (no PR)"] and jira.moved() == ["DFP-31"]


def test_no_current_versions_releases_nothing():
    jira = FakeJira({"DFP-32": [RFR, "Task"]})
    lines, moved, failed = release_issues(client(jira), source())
    assert lines == ["DFP-32: left, no current versions are configured (--versions), nothing is released"]
    assert jira.writes() == [] and moved == [] and not failed


def test_a_fix_version_changed_to_planned_meanwhile_is_caught_on_the_re_read():
    jira = FakeJira({"DFP-33": [RFR, "Task"]})
    real = jira._row

    def row(key, status, kind, fixes=("0.2.1",)):
        reads = sum(1 for m, p, _ in jira.calls if p.startswith("/rest/api/3/search/jql"))
        return real(key, status, kind, fixes if reads < 2 else ("0.2.2",))

    jira._row = row
    lines, moved, _ = release_issues(client(jira), source(), versions=CURRENT)
    assert lines == ["DFP-33: left, Fix Version 0.2.2 is not a current version (0.2.1, 0.2.1.2), so it is planned "
                     "work (read again)"]  # fmt: skip
    assert jira.writes() == [] and moved == []


def test_parse_versions_drops_blanks():
    from atl_agent.release import parse_versions

    assert parse_versions(" 0.2.1, ,0.2.1.2 ,") == frozenset({"0.2.1", "0.2.1.2"})
    assert parse_versions("") == frozenset()


def test_cli_needs_versions_and_an_empty_list_reads_nothing(capsys):
    jira = FakeJira({"DFP-34": [RFR, "Task"]})
    with pytest.raises(SystemExit) as exit_:
        main(["--agent", "sherif"], ENV, jira, no_keychain, source())
    assert exit_.value.code == 2
    assert main(["--agent", "sherif", "--versions", " , "], ENV, jira, no_keychain, source()) == 2
    assert "--versions is empty, nothing released" in capsys.readouterr().err
    assert jira.calls == []


def test_cli_leaves_a_planned_issue_in_dry_run_output(capsys):
    jira = FakeJira({"DFP-35": [RFR, "Task", ["0.2.2"]], "DFP-36": [RFR, "Task", ["0.2.1.2"]]})
    assert (
        main(["--agent", "sherif", "--versions", "0.2.1,0.2.1.2", "--dry-run"], ENV, jira, no_keychain, source()) == 0
    )
    out = capsys.readouterr().out
    assert "DFP-35: left, Fix Version 0.2.2 is not a current version" in out
    assert "DFP-36: would move Ready for Release -> Released (no PR)" in out
