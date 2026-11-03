"""BASITA-162: Talia's merge on Ahmed's delegation (plan 10.9, D22, D23). Fakes only: no token, no network."""

import base64
import copy
import datetime as dt
import json

import pytest

from agent_gh import delegation as d
from agent_gh.identity import IdentityError

UTC = dt.timezone.utc  # noqa: UP017 - matches the module, which runs on Python 3.9
OPS = "basitasolutions/basita-ops"
FARM = "basitasolutions/elbayoumy-farm-nutrition-platform"
PLAYBOOK = "ahmedoraby-eng/engineering-playbook"
NOW = "Tue, 03 Nov 2026 15:00:00 GMT"  # 17:00 in Cairo
HEAD = "a" * 40
OPS_RULES = {"owner": "ahmedoraby-eng", "critical_paths": [r"\.(sh|py|conf|plist)$", r"^\.github/", r"^\.claude/"]}
FARM_RULES = {
    "critical_paths": [r"^\.github/", r"^scripts/"],
    "jira_status": {
        "context": "Jira status",
        "projects": {"DFP": ["Ready for Release"], "ELB": ["In Progress"]},
        "waiver_label": "jira-status-waived",
    },
}


def issue(number=7, title="Delegation until 19:00", by="ahmedoraby-eng", created="2026-11-03T13:00:00Z", **extra):
    return {
        "number": number,
        "title": title,
        "user": {"login": by},
        "labels": [{"name": "delegation"}],
        "created_at": created,
        "closed_at": None,
        "html_url": f"https://github.com/{OPS}/issues/{number}",
        **extra,
    }


class FakeGitHub:
    """Answers by (method, path without the paging suffix). Records every call."""

    def __init__(self, repo=OPS, rules=OPS_RULES, issues=None, events=None, **pr):
        self.calls = []
        self.repo = repo
        self.pr = {
            "number": 12,
            "title": "BASITA-1: notes",
            "state": "open",
            "draft": False,
            "base": {"ref": "main"},
            "head": {"sha": HEAD, "ref": "sherif/BASITA-1-notes"},
            "user": {"login": "basita-sherif[bot]"},
            "mergeable": True,
            "mergeable_state": "clean",
            "changed_files": 1,
            "labels": [],
            **pr,
        }
        self.issues = [issue()] if issues is None else issues
        self.events = events or {}
        self.runs = [
            {"name": "Architecture review", "status": "completed", "conclusion": "success"},
            {"name": "Owner review", "status": "completed", "conclusion": "success"},
            {"name": "ci", "status": "completed", "conclusion": "skipped"},
        ]
        self.statuses = []
        self.files = [{"filename": "work/sherif/notes.md"}]
        self.commits = [{"author": {"login": "basita-sherif[bot]"}, "commit": {"author": {"email": "x"}}}]
        self.rules = rules
        self.merged = None

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        base = path.split("&per_page")[0].split("?per_page")[0]
        r = f"/repos/{self.repo}"
        date = {"Date": NOW}
        if method == "GET" and base == f"{r}/pulls/12":
            return date, self.pr
        if method == "GET" and base.startswith(f"/repos/{OPS}/issues?labels=delegation"):
            return date, self.issues
        if method == "GET" and base.startswith(f"/repos/{OPS}/issues/") and base.endswith("/events"):
            return date, self.events.get(int(base.split("/")[-2]), [])
        if method == "GET" and base == f"{r}/commits/{HEAD}/check-runs?filter=latest":
            return date, {"check_runs": self.runs}
        if method == "GET" and base == f"{r}/commits/{HEAD}/status":
            return date, {"statuses": self.statuses}
        if method == "GET" and base == f"{r}/contents/.github/review-rules.json?ref=main":
            return date, {"content": base64.b64encode(json.dumps(self.rules).encode()).decode()}
        if method == "GET" and base == f"{r}/pulls/12/files":
            return date, self.files
        if method == "GET" and base == f"{r}/pulls/12/commits":
            return date, self.commits
        if method == "PUT" and base == f"{r}/pulls/12/merge":
            self.merged = body
            return date, {"sha": "b" * 40, "merged": True}
        if method == "POST":
            return date, {}
        raise IdentityError(f"GitHub said 404 to {method} {path}")


def jira(statuses=None):
    def status(key):
        return (statuses or {}).get(key, "In Progress")

    return status


def checked(github, repo=OPS):
    return d.check_merge(repo, 12, github, jira(), sleep=lambda _: None)


# --- the window ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "created", "end"),
    [
        ("Delegation until 19:00", "2026-11-03T13:00:00Z", "2026-11-03T17:00:00+00:00"),  # 15:00 -> 19:00 Cairo
        ("Delegation until 09:00", "2026-11-03T13:00:00Z", "2026-11-04T01:00:00+00:00"),  # next morning, capped at 12 h
        ("Delegation until 2026-11-03 16:00", "2026-11-03T13:00:00Z", "2026-11-03T14:00:00+00:00"),
        ("Delegation until 20:00", "2026-07-01T13:00:00Z", "2026-07-01T17:00:00+00:00"),  # summer time, +3
    ],
)
def test_the_window_ends_at_the_cairo_time_or_after_twelve_hours(title, created, end):
    window, why = d.delegation_window(issue(title=title, created=created), [])
    assert why == "" and window.end == dt.datetime.fromisoformat(end)


def test_closing_the_issue_ends_the_window():
    window, _ = d.delegation_window(issue(closed_at="2026-11-03T14:00:00Z", state="closed"), [])
    assert window.end == dt.datetime(2026, 11, 3, 14, tzinfo=UTC)


def test_a_reopened_delegation_stays_ended_at_its_first_close():
    """Yousef's probe on #36: closed at 14:00 UTC, then reopened, GitHub clears closed_at; it must stay ended."""
    events = [
        {"event": "closed", "created_at": "2026-11-03T14:00:00Z", "actor": {"login": "ahmedoraby-eng"}},
        {"event": "reopened", "created_at": "2026-11-03T14:10:00Z", "actor": {"login": "ahmedoraby-eng"}},
        {"event": "closed", "created_at": "2026-11-03T16:00:00Z", "actor": {"login": "ahmedoraby-eng"}},
        {"event": "reopened", "created_at": "2026-11-03T16:05:00Z", "actor": {"login": "ahmedoraby-eng"}},
    ]
    window, _ = d.delegation_window(issue(), events)  # open again, closed_at None
    assert window.end == dt.datetime(2026, 11, 3, 14, tzinfo=UTC)
    github = FakeGitHub(events={7: events})
    assert any("no valid delegation now" in f for f in checked(github).failures)  # GitHub says 15:00
    assert d.covering(github, dt.datetime(2026, 11, 3, 14, 30, tzinfo=UTC)) is None


@pytest.mark.parametrize(
    ("changes", "events", "why"),
    [
        ({"user": {"login": "basita-talia[bot]"}}, [], "opened by basita-talia[bot]"),
        ({"user": {"login": "abdelrahman"}}, [], "not Ahmed or basita-sync"),
        ({"labels": []}, [], "no 'delegation' label"),
        ({"title": "Delegation for the afternoon"}, [], "title isn't"),
        ({"pull_request": {}}, [], "is a pull request"),
        ({}, [{"event": "renamed", "actor": {"login": "basita-sync[bot]"}}], "renamed by basita-sync[bot]"),
    ],
)
def test_what_is_not_a_delegation(changes, events, why):
    window, reason = d.delegation_window({**issue(), **changes}, events)
    assert window is None and why in reason


def test_a_delegation_recorded_from_the_mac_counts_and_ahmed_may_rename_his_own():
    window, _ = d.delegation_window(issue(by="basita-sync[bot]"), [])
    assert window.opened_by == "basita-sync[bot]"
    window, _ = d.delegation_window(issue(), [{"event": "renamed", "actor": {"login": "ahmedoraby-eng"}}])
    assert window is not None


def test_covering_finds_the_window_that_held_a_moment():
    github = FakeGitHub()
    assert d.covering(github, dt.datetime(2026, 11, 3, 16, tzinfo=UTC)).number == 7
    assert d.covering(github, dt.datetime(2026, 11, 3, 18, tzinfo=UTC)) is None
    assert d.covering(github, dt.datetime(2026, 11, 3, 12, tzinfo=UTC)) is None


# --- the checks ---------------------------------------------------------------------------------------------


def test_every_check_holds_on_a_docs_pr():
    result = checked(FakeGitHub())
    assert result.failures == [] and result.delegation.number == 7 and result.head == HEAD
    assert any("no Jira status check on this repository" in line for line in result.passed)


def test_now_is_githubs_clock_and_an_expired_delegation_refuses():
    github = FakeGitHub(issues=[issue(title="Delegation until 16:30")])  # ended 14:30 UTC; GitHub says 15:00
    assert any("no valid delegation now" in f for f in checked(github).failures)


@pytest.mark.parametrize(
    ("pr", "why"),
    [
        ({"state": "closed"}, "it is closed"),
        ({"base": {"ref": "release"}}, "targets release"),
        ({"draft": True}, "a draft"),
        ({"mergeable": False, "mergeable_state": "dirty"}, "can't merge it cleanly"),
        ({"mergeable": None}, "can't merge it cleanly"),
        ({"user": {"login": "basita-talia[bot]"}}, "Talia's own PR"),
    ],
)
def test_pr_refusals(pr, why):
    assert any(why in f for f in checked(FakeGitHub(**pr)).failures)


def test_unknown_mergeable_is_asked_again():
    github = FakeGitHub(mergeable=None)
    checked(github)
    assert sum(1 for m, p, _ in github.calls if p == f"/repos/{OPS}/pulls/12") == 4


@pytest.mark.parametrize(
    ("runs", "statuses", "why"),
    [
        ([{"name": "Architecture review", "status": "completed", "conclusion": "failure"}], [], "Architecture review"),
        ([{"name": "Architecture review", "status": "in_progress", "conclusion": None}], [], "in_progress"),
        ([{"name": "Owner review", "status": "completed", "conclusion": "success"}], [], "Architecture review is not"),
        (None, [{"context": "ci/x", "state": "pending"}], "ci/x (pending)"),
    ],
)
def test_check_refusals(runs, statuses, why):
    github = FakeGitHub()
    if runs is not None:
        github.runs = runs
    github.statuses = statuses
    assert any(why in f for f in checked(github).failures)


@pytest.mark.parametrize(
    ("files", "changed", "why"),
    [
        ([{"filename": "sync.sh"}], 1, "critical paths, these wait for Ahmed: sync.sh"),
        ([{"filename": "work/x.md", "previous_filename": ".github/x.yml"}], 1, ".github/x.yml"),
        ([{"filename": "work/x.md"}], 2, "listed 1 of 2 files"),
    ],
)
def test_critical_paths_wait_for_ahmed(files, changed, why):
    github = FakeGitHub(changed_files=changed)
    github.files = files
    assert any(why in f for f in checked(github).failures)


def test_rules_without_critical_paths_refuse_everything():
    assert any("no critical paths" in f for f in checked(FakeGitHub(rules={"owner": "x"})).failures)


def test_a_commit_by_talias_bot_makes_it_her_own():
    github = FakeGitHub()
    github.commits.append({"commit": {"author": {"email": "351996843+basita-talia[bot]@users.noreply.github.com"}}})
    assert any("Talia's own PR" in f for f in checked(github).failures)


# --- Jira status (D23) --------------------------------------------------------------------------------------


def farm(**pr):
    github = FakeGitHub(repo=FARM, rules=FARM_RULES, title="DFP-7: x", head={"sha": HEAD, "ref": "malek/DFP-7-x"}, **pr)
    github.statuses = [{"context": "Jira status", "state": "success", "creator": {"login": "github-actions[bot]"}}]
    return github


def test_jira_status_green_and_read_again():
    result = d.check_merge(FARM, 12, farm(), jira({"DFP-7": "Ready for Release"}), sleep=lambda _: None)
    assert result.failures == [] and any("Jira read again: DFP-7" in p for p in result.passed)


def test_jira_read_again_refuses_a_key_that_moved():
    result = d.check_merge(FARM, 12, farm(), jira({"DFP-7": "In QA"}), sleep=lambda _: None)
    assert any("DFP-7 is In QA" in f for f in result.failures)


@pytest.mark.parametrize(
    ("status", "why"),
    [
        ([], "no 'Jira status' status"),
        (
            [{"context": "Jira status", "state": "success", "creator": {"login": "basita-malek[bot]"}}],
            "by basita-malek",
        ),
        ([{"context": "Jira status", "state": "failure", "creator": {"login": "github-actions[bot]"}}], "failure"),
    ],
)
def test_jira_status_refusals(status, why):
    github = farm()
    github.statuses = status
    result = d.check_merge(FARM, 12, github, jira({"DFP-7": "Ready for Release"}), sleep=lambda _: None)
    assert any(why in f for f in result.failures)


def test_a_waived_jira_status_waits_for_ahmed():
    github = farm(labels=[{"name": "jira-status-waived"}])
    result = d.check_merge(FARM, 12, github, jira({"DFP-7": "Ready for Release"}), sleep=lambda _: None)
    assert any("Ahmed waived" in f for f in result.failures)


# --- repositories, merge and trace ------------------------------------------------------------------------------


def test_only_the_three_repositories():
    with pytest.raises(IdentityError, match="works on basitasolutions/basita-ops"):
        d.check_merge("someone/else", 12, FakeGitHub(), jira())
    assert checked(FakeGitHub(repo=PLAYBOOK), PLAYBOOK).failures == []  # D23: all repositories


def test_the_merge_is_a_squash_with_the_head_pinned_and_the_trace():
    github = FakeGitHub()
    result = checked(github)
    answer = d.merge(result, github)
    assert github.merged["merge_method"] == "squash" and github.merged["sha"] == HEAD
    assert github.merged["commit_message"].startswith(
        f"Merged by Talia on Ahmed's delegation: https://github.com/{OPS}/issues/7"
    )
    notes = d.trace(result, answer, github)
    assert notes == ["ok label merged-on-delegation", "ok comment"]
    posts = [(p, b) for m, p, b in github.calls if m == "POST"]
    assert posts[0] == (f"/repos/{OPS}/issues/12/labels", {"labels": ["merged-on-delegation"]})


def test_merge_refuses_a_failed_check():
    result = checked(FakeGitHub(draft=True))
    with pytest.raises(IdentityError, match="refused: the PR: it is a draft"):
        d.merge(result, FakeGitHub())


# --- the command line ---------------------------------------------------------------------------------------------

CONFIG = {
    "agents": {
        "reader": {"kind": "app", "read_only": True, "permissions": {"contents": "read"}},
        "talia": {"kind": "app"},
    }
}


def run_cli(agent, args, github, **extra):
    out, minted, revoked = [], [], []

    def token_for(agent, repo, permissions, config, transport):
        minted.append((agent, repo, tuple(sorted(permissions.items()))))
        return f"tok-{agent}-{len(minted)}", True

    deps = {
        "api": lambda transport, method, path, auth, body=None: github(method, path, body),
        "transport": None,
        "token_for": token_for,
        "revoke": lambda token, transport: revoked.append(token),
        "read_permissions": lambda entry: entry["permissions"],
        "jira": jira(),
        "out": out.append,
        **extra,
    }
    code = d.cli(agent, args, copy.deepcopy(CONFIG), OPS, deps)
    return code, out, minted, revoked


def test_cli_merge_check_merges_nothing_and_revokes_its_tokens():
    github = FakeGitHub()
    code, out, minted, revoked = run_cli("talia", ["merge-check", "12"], github)
    assert code == 0 and github.merged is None and "every check holds" in out[-1]
    assert {m[0] for m in minted} == {"reader"} and len(revoked) == len(minted)


def test_cli_merge_reads_as_reader_and_writes_as_talia():
    github = FakeGitHub()
    code, out, minted, revoked = run_cli("talia", ["pr", "merge", "12", "-R", OPS], github)
    assert code == 0 and github.merged and any(line.startswith("merged basitasolutions/basita-ops#12") for line in out)
    talia = [m for m in minted if m[0] == "talia"]
    assert talia == [("talia", OPS, (("contents", "write"), ("metadata", "read"), ("pull_requests", "write")))]
    assert len(revoked) == len(minted)


@pytest.mark.parametrize("args", [["pr", "merge", "12", "--admin"], ["pr", "merge"], ["pr", "merge", "12", "-R"]])
def test_cli_takes_a_number_and_repo_only(args):
    with pytest.raises(IdentityError):
        run_cli("talia", args, FakeGitHub())


def test_cli_failed_checks_exit_one_and_merge_nothing():
    github = FakeGitHub(draft=True)
    code, out, _, _ = run_cli("talia", ["pr", "merge", "12"], github)
    assert code == 1 and github.merged is None and any(line.startswith("FAIL the PR") for line in out)


def test_cli_dry_run_mints_nothing():
    code, out, minted, _ = run_cli("talia", ["pr", "merge", "12"], FakeGitHub(), dry_run=True)
    assert code == 0 and minted == [] and "dry run" in out[0]


def test_cli_delegation_covers():
    code, out, _, _ = run_cli("reader", ["delegation-covers", "2026-11-03T16:00:00Z"], FakeGitHub())
    assert code == 0 and out[0].startswith(f"https://github.com/{OPS}/issues/7 2026-11-03T13:00:00Z")
    code, out, _, _ = run_cli("reader", ["delegation-covers", "2026-11-03T18:00:00Z"], FakeGitHub())
    assert code == 1


def test_only_talia_merges_and_only_reader_answers_covers():
    assert d.is_delegation_command("talia", ["pr", "merge", "3"])
    assert d.is_delegation_command("talia", ["merge-check", "3"])
    assert d.is_delegation_command("reader", ["delegation-covers", "x"])
    assert not d.is_delegation_command("malek", ["pr", "merge", "3"])
    assert not d.is_delegation_command("sync", ["pr", "merge", "3"])
    assert not d.is_delegation_command("talia", ["delegation-covers", "x"])


# --- basita-sync records a delegation from Ahmed's words on his Mac (D22) ------------------------------------------


class SyncGitHub(FakeGitHub):
    def __call__(self, method, path, body):
        if method == "GET" and path == f"/repos/{OPS}":
            self.calls.append((method, path, body))
            return {"Date": NOW}, {}
        if method == "GET" and path == f"/repos/{OPS}/issues/7":
            self.calls.append((method, path, body))
            return {"Date": NOW}, issue()
        if method == "GET" and path == f"/repos/{OPS}/issues/8":
            self.calls.append((method, path, body))
            return {"Date": NOW}, {**issue(8), "labels": []}
        if method == "POST" and path == f"/repos/{OPS}/issues":
            self.calls.append((method, path, body))
            return {"Date": NOW}, {"html_url": f"https://github.com/{OPS}/issues/9"}
        return super().__call__(method, path, body)


def test_sync_records_a_delegation_with_ahmeds_words():
    github = SyncGitHub()
    code, out, minted, revoked = run_cli(
        "sync", ["delegation-record", "--until", "21:00"], github, stdin=lambda: "خدي كل الصلاحيات\nلحد ما ارجع"
    )
    assert code == 0 and out == [f"recorded https://github.com/{OPS}/issues/9 until 2026-11-03 19:00 UTC"]
    (post,) = [b for m, p, b in github.calls if m == "POST"]
    assert post["title"] == "Delegation until 21:00" and post["labels"] == ["delegation"]
    assert "> خدي كل الصلاحيات\n> لحد ما ارجع" in post["body"]
    assert ("sync", OPS, (("issues", "write"), ("metadata", "read"))) in minted and len(revoked) == len(minted)


@pytest.mark.parametrize(
    ("args", "stdin", "why"),
    [
        (["delegation-record", "--until", "08:00"], "x", "not within the next 12 hours"),  # 06:00 UTC tomorrow
        (["delegation-record", "--until", "soon"], "x", "not HH:MM"),
        (["delegation-record", "--until", "21:00"], "  ", "nothing on stdin"),
        (["delegation-record", "21:00"], "x", "usage"),
        (["delegation-note", "8"], "merged #12", "not a delegation issue"),
    ],
)
def test_sync_refusals(args, stdin, why):
    with pytest.raises(IdentityError, match=why):
        run_cli("sync", args, SyncGitHub(), stdin=lambda: stdin)


def test_sync_notes_a_merge_on_the_delegation_issue():
    github = SyncGitHub()
    code, out, _, _ = run_cli("sync", ["delegation-note", "7"], github, stdin=lambda: "merged basita-ops#12")
    assert code == 0 and out[0] == f"noted on https://github.com/{OPS}/issues/7"
    assert (f"/repos/{OPS}/issues/7/comments", {"body": "merged basita-ops#12"}) in [
        (p, b) for m, p, b in github.calls if m == "POST"
    ]
    assert d.is_delegation_command("sync", ["delegation-note", "7"]) and not d.is_delegation_command(
        "sync", ["pr", "merge"]
    )


def test_a_delegated_merge_from_a_cloud_session_waits_for_m0(monkeypatch):
    """With BASITA-163 on main: the merge and the record are writes, gated like any other before a token."""
    from agent_gh import __main__ as cli

    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.delenv("AGENT_GH_CLOUD_WRITES", raising=False)
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: pytest.fail("a token was minted"))
    for agent, args in (("talia", ["pr", "merge", "12", "-R", OPS]), ("sync", ["delegation-note", "7"])):
        with pytest.raises(IdentityError, match="waits for the proxy test"):
            cli.cmd_gh(agent, args)
