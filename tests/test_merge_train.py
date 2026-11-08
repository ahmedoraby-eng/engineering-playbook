"""ADR-020 E1: the merge train brings only the next pull request up to date, and never merges."""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import urllib.error
from pathlib import Path

import pytest

from merge_train import Options, parse, run
from merge_train.__main__ import main as cli
from merge_train.config import ConfigError
from merge_train.conflicts import conflict_files
from merge_train.github import CheckRun, GitHub, GitHubError, PullRequest
from merge_train.train import CONFLICT_LABEL, E2E_LABEL, marker

SHA = "a" * 40
MAIN = "b" * 40
NEW = "c" * 40


def pr(number: int, **kw) -> PullRequest:
    fields = {
        "state": "open",
        "merged": False,
        "draft": False,
        "title": f"PR {number}",
        "base_ref": "main",
        "head_ref": f"feature-{number}",
        "head_sha": SHA,
        "labels": frozenset(),
    }
    fields.update(kw)
    return PullRequest(number=number, **fields)


def check(name: str, conclusion: str = "success", status: str = "completed", started: str = "2026-11-09T10:00:00Z"):
    return CheckRun(name=name, status=status, conclusion=conclusion, started_at=started)


GREEN = [check("ci / Build and test"), check("Owner review")]


class FakeGH:
    """Reads come from dicts; every write is recorded. It has no merge method, like GitHub."""

    def __init__(self, prs, *, behind=0, runs=None, comments=None, update=True, moved_to=NEW):
        self.prs = {p.number: p for p in prs}
        self.behind = behind
        self.runs = runs if runs is not None else {SHA: list(GREEN), NEW: list(GREEN)}
        self.said = {p.number: list((comments or {}).get(p.number, [])) for p in prs}
        self.update = update
        self.moved_to = moved_to
        self.writes: list[tuple] = []
        self.commit_age = 1000.0

    def default_branch(self):
        return "main"

    def pull(self, n):
        return self.prs.get(n)

    def comments(self, n):
        return list(self.said[n])

    def behind_by(self, base, sha):
        return self.behind

    def branch_sha(self, branch):
        return MAIN

    def commit_time(self, sha):
        return 10_000.0 - self.commit_age

    def check_runs(self, sha):
        return list(self.runs.get(sha, []))

    def update_branch(self, n, sha):
        self.writes.append(("update", n, sha))
        if self.update:
            p = self.prs[n]
            self.prs[n] = PullRequest(**{**p.__dict__, "head_sha": self.moved_to or p.head_sha})
        return self.update

    def add_label(self, n, label):
        self.writes.append(("add_label", n, label))
        p = self.prs[n]
        self.prs[n] = PullRequest(**{**p.__dict__, "labels": p.labels | {label}})

    def remove_label(self, n, label):
        self.writes.append(("remove_label", n, label))
        p = self.prs[n]
        self.prs[n] = PullRequest(**{**p.__dict__, "labels": p.labels - {label}})

    def comment(self, n, body):
        self.writes.append(("comment", n, body))
        self.said[n].append(body)


def go(gh, order, **kw):
    kw.setdefault("now", lambda: 10_000.0)
    kw.setdefault("sleep", lambda s: None)
    return run(gh, parse(json.dumps({"wave": "w", "order": order})), **kw)


def kinds(gh):
    return [w[0] for w in gh.writes]


# --- config -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, message",
    [
        ("not json", "not valid JSON"),
        ("[1]", "JSON object"),
        ('{"wave": "x"}', "'order' list"),
        ('{"order": [1, "2"]}', "positive pull request number"),
        ('{"order": [0]}', "positive pull request number"),
        ('{"order": [true]}', "positive pull request number"),
        ('{"order": [3, 4, 3]}', "#3 more than once"),
        ('{"order": [1], "wave": 7}', "'wave' must be a string"),
    ],
)
def test_config_refuses_what_it_cannot_trust(text, message):
    with pytest.raises(ConfigError, match=message):
        parse(text)


def test_config_reads_the_order():
    cfg = parse('{"wave": " 0.2.2-w1 ", "order": [226, 227]}')
    assert (cfg.wave, cfg.order) == ("0.2.2-w1", (226, 227))
    assert parse('{"order": []}').order == ()


# --- who is the head ---------------------------------------------------------------------------------


def test_an_empty_order_does_nothing():
    gh = FakeGH([])
    report = go(gh, [])
    assert gh.writes == [] and "empty" in report.actions[0]


def test_head_is_the_first_open_pull_request_skipping_merged_closed_and_missing():
    gh = FakeGH(
        [pr(1, state="closed", merged=True), pr(2, state="closed"), pr(4), pr(5)],
        behind=2,
    )
    report = go(gh, [1, 2, 3, 4, 5])
    assert report.head == 4
    states = {n: s for n, s, _ in report.rows}
    assert states == {1: "merged", 2: "closed", 3: "not found", 4: "head of the train", 5: "waiting"}
    assert ("update", 4, SHA) in gh.writes
    assert all(w[1] != 5 for w in gh.writes), "only the next pull request is touched"


def test_every_pull_request_merged_means_the_train_is_done():
    gh = FakeGH([pr(1, state="closed", merged=True)])
    assert "train is empty" in go(gh, [1]).actions[0]
    assert gh.writes == []


def test_a_draft_head_is_left_alone():
    gh = FakeGH([pr(7, draft=True)], behind=3)
    report = go(gh, [7])
    assert gh.writes == []
    assert report.rows[0][2].startswith("draft")


def test_a_stacked_head_waits_for_its_base():
    gh = FakeGH([pr(8, base_ref="malek/other-branch")], behind=3)
    report = go(gh, [8])
    assert gh.writes == []
    assert "stacked on malek/other-branch" in report.actions[0]


# --- bringing the head up to date --------------------------------------------------------------------


def test_behind_head_is_updated_with_its_current_sha_then_e2e_is_requested_once():
    gh = FakeGH([pr(10, labels=frozenset({E2E_LABEL}))], behind=4, runs={NEW: list(GREEN)})
    go(gh, [10])
    assert gh.writes[0] == ("update", 10, SHA)
    # run-e2e is on the PR already, so it is taken off and put back: adding it again fires nothing
    assert gh.writes[1:3] == [("remove_label", 10, E2E_LABEL), ("add_label", 10, E2E_LABEL)]
    assert marker("e2e", NEW) in gh.writes[3][2]
    assert kinds(gh).count("update") == 1


def test_no_e2e_is_requested_for_a_pull_request_that_never_asked_for_one():
    gh = FakeGH([pr(11)], behind=1)
    go(gh, [11])
    assert E2E_LABEL not in [w[2] for w in gh.writes if w[0] in ("add_label", "remove_label")]


def test_the_new_head_is_waited_for_and_its_checks_get_time_to_appear():
    slept: list[float] = []
    gh = FakeGH([pr(12)], behind=1)
    gh.commit_age = 5.0
    go(gh, [12], sleep=slept.append, now=lambda: 10_000.0)
    assert any(s >= 100 for s in slept), "waits out most of the settle time (120 s) on a 5 s old commit"


def test_head_that_did_not_move_after_the_update_says_nothing_about_readiness():
    gh = FakeGH([pr(13)], behind=1, moved_to="")  # GitHub accepted, but the merge commit is not there yet
    report = go(gh, [13])
    assert kinds(gh) == ["update"]
    assert "not visible yet" in report.actions[-1]


def test_a_conflict_is_reported_once_with_the_files_and_the_label():
    gh = FakeGH([pr(14)], behind=2, update=False)
    seen = []
    go(gh, [14], conflict_files=lambda base, head: seen.append((base, head)) or ["src/a.ts", "i18n/`x`\n.json"])
    assert seen == [(MAIN, SHA)]
    comment = next(w for w in gh.writes if w[0] == "comment")[2]
    assert marker("conflict", f"{SHA[:7]}-{MAIN[:7]}") in comment
    assert "`src/a.ts`" in comment and "`i18n/x.json`" in comment, "backticks and newlines of a file name are removed"
    assert "Malek" in comment
    assert ("add_label", 14, CONFLICT_LABEL) in gh.writes

    before = len(gh.writes)
    go(gh, [14], conflict_files=lambda base, head: ["src/a.ts"])
    assert [w[0] for w in gh.writes[before:]] == ["update"], "a second run adds no comment and no label"


def test_a_conflict_without_a_file_list_still_reports():
    gh = FakeGH([pr(15)], behind=2, update=False)
    go(gh, [15])
    assert "تعذّر سرد الملفات" in next(w for w in gh.writes if w[0] == "comment")[2]


def test_the_conflict_label_goes_when_the_pull_request_is_current_again():
    gh = FakeGH([pr(16, labels=frozenset({CONFLICT_LABEL}))], behind=0)
    go(gh, [16])
    assert ("remove_label", 16, CONFLICT_LABEL) in gh.writes


def test_dry_run_changes_nothing():
    gh = FakeGH([pr(17, labels=frozenset({E2E_LABEL}))], behind=2)
    report = go(gh, [17], options=Options(dry_run=True))
    assert gh.writes == [] and "dry run" in report.actions[-1]


# --- E2E on a current head ---------------------------------------------------------------------------


def test_a_current_head_without_an_e2e_run_gets_one_request_per_head():
    labels = frozenset({E2E_LABEL})
    gh = FakeGH([pr(20, labels=labels)], runs={SHA: list(GREEN)})
    go(gh, [20])
    assert kinds(gh) == ["remove_label", "add_label", "comment"]
    again = len(gh.writes)
    go(gh, [20])
    assert len(gh.writes) == again, "the marker keeps the second run from requesting E2E again"


def test_a_running_or_failed_e2e_is_not_ready():
    for status, conclusion in (("in_progress", ""), ("completed", "failure")):
        runs = {SHA: [*GREEN, check("E2E (Playwright)", conclusion, status)]}
        gh = FakeGH([pr(21, labels=frozenset({E2E_LABEL}))], runs=runs)
        go(gh, [21])
        assert gh.writes == []


def test_a_skipped_e2e_run_is_a_filter_not_a_result():
    runs = {SHA: [*GREEN, check("E2E (Playwright)", "skipped")]}
    gh = FakeGH([pr(22, labels=frozenset({E2E_LABEL}))], runs=runs)
    go(gh, [22])
    assert "remove_label" in kinds(gh), "a skipped run counts as no run, so E2E is requested"


# --- ready -------------------------------------------------------------------------------------------


def test_ready_is_announced_once_per_head_and_the_train_never_merges():
    labels = frozenset({E2E_LABEL, "reviewed-by-ahmed"})
    runs = {SHA: [*GREEN, check("E2E (Playwright)"), check("Jira status", "failure")]}
    gh = FakeGH([pr(30, labels=labels)], runs=runs)
    go(gh, [30])
    assert kinds(gh) == ["comment"]
    text = gh.writes[0][2]
    assert marker("ready", SHA) in text and "جاهز للدمج: #30" in text
    assert "CI ✓" in text and "E2E ✓" in text and "Owner review ✓" in text and "reviewed-by-ahmed ✓" in text
    assert "Jira status" in text, "a red check that is not required is named, not hidden"
    assert "لا يدمج" in text
    go(gh, [30])
    assert kinds(gh) == ["comment"], "announced once"
    assert not any("merge" in k for k in kinds(gh))


def test_a_new_head_is_announced_again():
    runs = {SHA: list(GREEN), NEW: list(GREEN)}
    gh = FakeGH([pr(31)], runs=runs)
    go(gh, [31])
    gh.prs[31] = pr(31, head_sha=NEW)
    go(gh, [31])
    assert kinds(gh) == ["comment", "comment"]


def test_documentation_only_pull_request_is_ready_without_ci():
    gh = FakeGH([pr(32)], runs={SHA: [check("Owner review")]})
    go(gh, [32])
    assert kinds(gh) == ["comment"]


@pytest.mark.parametrize(
    "runs",
    [
        [check("ci / Build and test", "", "in_progress"), check("Owner review")],
        [check("ci / Build and test", "failure"), check("Owner review")],
        [check("ci / Build and test"), check("Owner review", "failure")],
        [check("ci / Build and test")],  # Owner review has not reported yet
        [],
    ],
)
def test_not_ready_while_a_required_check_is_not_green(runs):
    gh = FakeGH([pr(33)], runs={SHA: runs})
    go(gh, [33])
    assert gh.writes == []


def test_an_old_green_e2e_on_the_previous_head_does_not_count():
    runs = {SHA: list(GREEN), "d" * 40: [check("E2E (Playwright)")]}
    gh = FakeGH([pr(34, labels=frozenset({E2E_LABEL}))], runs=runs)
    go(gh, [34])
    assert "comment" in kinds(gh) and "جاهز" not in gh.writes[-1][2]


# --- the client: what it may call --------------------------------------------------------------------


class Response:
    def __init__(self, status, payload):
        self.status = status
        self._raw = json.dumps(payload).encode() if payload is not None else b""

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def client(handler):
    calls = []

    def transport(request):
        calls.append((request.get_method(), request.full_url, request.get_header("Authorization"), request.data))
        out = handler(request.get_method(), request.full_url)
        if isinstance(out, int):
            raise urllib.error.HTTPError(request.full_url, out, "x", {}, io.BytesIO(b'{"message": "no"}'))
        return out

    return GitHub("o/r", "READ", "WRITE", transport=transport, sleep=lambda s: None), calls


def test_writes_use_the_app_token_and_reads_use_the_read_token_and_nothing_can_merge():
    status = {"GET": 200, "DELETE": 204}
    gh, calls = client(lambda m, u: Response(status.get(m, 201), {"default_branch": "main"} if m == "GET" else {}))
    gh.default_branch()
    gh.add_label(1, "x")
    gh.comment(1, "hi")
    gh.remove_label(1, "x")
    assert {(m, a) for m, _, a, _ in calls if m == "GET"} == {("GET", "Bearer READ")}
    assert {(m, a) for m, _, a, _ in calls if m != "GET"} == {
        ("POST", "Bearer WRITE"),
        ("DELETE", "Bearer WRITE"),
    }
    public = {n for n in dir(GitHub) if not n.startswith("_")}
    writers = {"update_branch", "add_label", "remove_label", "comment"}
    assert not any("merge" in n for n in public)
    assert writers <= public


def test_update_branch_outcomes():
    gh, calls = client(lambda m, u: Response(202, {"message": "Updating pull request branch."}))
    assert gh.update_branch(5, SHA) is True
    assert json.loads(calls[0][3]) == {"expected_head_sha": SHA}
    assert calls[0][1].endswith("/repos/o/r/pulls/5/update-branch")

    def conflict(m, u):
        body = io.BytesIO(b'{"message": "merge conflict between base and head"}')
        raise urllib.error.HTTPError(u, 422, "x", {}, body)

    gh, _ = client(conflict)
    assert gh.update_branch(5, SHA) is False
    gh, _ = client(lambda m, u: 422)  # "expected head sha didn't match": not a conflict
    with pytest.raises(GitHubError, match="422"):
        gh.update_branch(5, SHA)
    gh, _ = client(lambda m, u: 403)
    with pytest.raises(GitHubError, match="403"):
        gh.update_branch(5, SHA)


def test_a_failed_read_names_the_call_but_never_the_token():
    gh, _ = client(lambda m, u: 500)
    with pytest.raises(GitHubError) as err:
        gh.default_branch()
    assert "READ" not in str(err.value) and "WRITE" not in str(err.value)


def test_transient_errors_are_retried():
    answers = iter([502, Response(200, {"default_branch": "main"})])
    gh, calls = client(lambda m, u: next(answers))
    assert gh.default_branch() == "main" and len(calls) == 2


def test_removing_a_label_that_is_gone_is_fine():
    gh, _ = client(lambda m, u: 404)
    gh.remove_label(1, "run-e2e")


def test_read_file_decodes_content_and_reports_a_missing_file():
    body = {"encoding": "base64", "content": base64.b64encode(b'{"order": []}').decode()}
    gh, calls = client(lambda m, u: Response(200, body))
    assert gh.read_file(".github/merge-train.json", "main") == '{"order": []}'
    assert calls[0][1].endswith("/contents/.github/merge-train.json?ref=main")
    gh, _ = client(lambda m, u: 404)
    assert gh.read_file("x", "main") is None


def test_pull_and_check_runs_are_parsed():
    pull = {
        "state": "open",
        "merged": False,
        "draft": True,
        "title": "t",
        "base": {"ref": "main"},
        "head": {"ref": "f", "sha": SHA},
        "labels": [{"name": "run-e2e"}],
    }
    gh, _ = client(lambda m, u: Response(200, pull))
    got = gh.pull(3)
    assert got.draft and got.labels == {"run-e2e"} and got.head_sha == SHA
    runs = {"check_runs": [{"name": "n", "status": "completed", "conclusion": "success", "started_at": "t"}]}
    gh, _ = client(lambda m, u: Response(200, runs))
    assert gh.check_runs(SHA) == [CheckRun("n", "completed", "success", "t")]


# --- conflicting files from git ----------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is needed")
def test_conflict_files_lists_only_the_conflicted_paths(tmp_path):
    version = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.split()[2].split(".")
    if tuple(int(v) for v in version[:2]) < (2, 38):
        pytest.skip("git merge-tree --write-tree needs git 2.38")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    (tmp_path / "b.txt").write_text("one\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "feature")
    (tmp_path / "a.txt").write_text("feature\n")
    (tmp_path / "b.txt").write_text("feature b\n")
    git(tmp_path, "commit", "-qam", "feature")
    git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "a.txt").write_text("main\n")
    git(tmp_path, "commit", "-qam", "main")
    main_sha, feature_sha = git(tmp_path, "rev-parse", "main"), git(tmp_path, "rev-parse", "feature")
    assert conflict_files(tmp_path, main_sha, feature_sha) == ["a.txt"]
    git(tmp_path, "checkout", "-q", "feature")
    git(tmp_path, "reset", "-q", "--hard", main_sha)
    assert conflict_files(tmp_path, main_sha, main_sha) == []
    assert conflict_files(tmp_path, "1" * 40, main_sha) is None


# --- the command line --------------------------------------------------------------------------------


def patch_cli(monkeypatch, text, *, prs=None):
    class Stub(FakeGH):
        def read_file(self, path, ref):
            return text

    stub = Stub(prs or [], behind=0)
    monkeypatch.setattr("merge_train.__main__.GitHub", lambda repo, r, w: stub)
    monkeypatch.setenv("GH_READ_TOKEN", "r")
    monkeypatch.setenv("GH_WRITE_TOKEN", "w")
    return stub


def test_cli_goes_red_on_a_bad_order_file(monkeypatch, capsys):
    patch_cli(monkeypatch, '{"order": ["x"]}')
    assert cli(["--repo", "o/r"]) == 1
    assert "::error title=Merge train::" in capsys.readouterr().out


def test_cli_with_no_order_file_does_nothing(monkeypatch, capsys):
    patch_cli(monkeypatch, None)
    assert cli(["--repo", "o/r"]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_cli_needs_both_tokens(monkeypatch, capsys):
    monkeypatch.delenv("GH_READ_TOKEN", raising=False)
    monkeypatch.delenv("GH_WRITE_TOKEN", raising=False)
    assert cli(["--repo", "o/r"]) == 2


def test_cli_writes_the_step_summary(monkeypatch, tmp_path, capsys):
    patch_cli(monkeypatch, '{"wave": "w1", "order": [1]}', prs=[pr(1, state="closed", merged=True)])
    out = tmp_path / "summary.md"
    assert cli(["--repo", "o/r", "--summary", str(out)]) == 0
    text = out.read_text()
    assert "### Merge train w1" in text and "| #1 | merged |" in text


def test_cli_reports_a_github_failure_as_red(monkeypatch, capsys):
    stub = patch_cli(monkeypatch, '{"order": [1]}')

    def boom(n):
        raise GitHubError("GET /pulls/1 -> 500")

    stub.pull = boom
    assert cli(["--repo", "o/r"]) == 1
    assert "500" in capsys.readouterr().out
