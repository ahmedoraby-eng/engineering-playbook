"""Batch mode of the merge train (Yousef's amendment D1-D9). A fake GitHub keeps train/run, the merges API,
E2E runs and main, so each test walks the real decisions run by run."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import defaultdict

import pytest

from merge_train import Options, parse, run
from merge_train.__main__ import main as cli
from merge_train.batch import (
    MAX_BATCH,
    PLAIN,
    RED_LABEL,
    Facts,
    MainStep,
    Member,
    assess,
    candidacy,
    main_moved_by,
    manifest_message,
    next_step,
    parse_manifest,
    select_batch,
    wave_token,
)
from merge_train.github import E2E_WORKFLOW, TRAIN_REF, CheckRun, GitCommit, MergeResult, PullRequest, WorkflowRun
from merge_train.train import CONFLICT_LABEL, E2E_LABEL, marker

BASE = hashlib.sha1(b"base").hexdigest()
E2E = "E2E (Playwright)"
GREEN = [
    CheckRun("ci / Build and test", "completed", "success", "2026-11-17T10:00:00Z", 1),
    CheckRun("Owner review", "completed", "success", "2026-11-17T10:00:00Z", 2),
]


def head(n: int, version: int = 0) -> str:
    return hashlib.sha1(f"head-{n}-{version}".encode()).hexdigest()


def tree_of(parent_tree: str, sha: str) -> str:
    return hashlib.sha1(f"{parent_tree}+{sha}".encode()).hexdigest()


def pr(n: int, **kw) -> PullRequest:
    fields = {
        "state": "open",
        "merged": False,
        "draft": False,
        "title": f"PR {n}",
        "base_ref": "main",
        "head_ref": f"f-{n}",
        "head_sha": head(n),
        "labels": frozenset({E2E_LABEL}),
    }
    fields.update(kw)
    return PullRequest(number=n, **fields)


class BatchGH:
    """GitHub as the batch train sees it. Every write is recorded; the batch writes check the fence like the
    real client (a wrong ref, base or workflow fails the test)."""

    def __init__(self, prs, *, files=None, conflicts=()):
        self.prs = {p.number: p for p in prs}
        self.main = BASE
        self.commits = {BASE: GitCommit(BASE, "base", "tree-0", ())}
        self.train: str | None = None
        self.checks: dict[str, list[CheckRun]] = {}
        self.history: dict[str, list[CheckRun]] = defaultdict(list)
        self.runs: dict[str, list[WorkflowRun]] = {}
        self.files = files or {}
        self.conflicts = set(conflicts)
        self.assoc: dict[str, list[int]] = {}
        self.said: dict[int, list[str]] = defaultdict(list)
        self.writes: list[tuple] = []
        self.ann: dict[int, list[str]] = {}
        self.behind: dict[int, int] = {}
        self.tip_age = 10_000.0
        self._ids = itertools.count(100)

    # reads
    def default_branch(self):
        return "main"

    def branch_sha(self, branch):
        assert branch == "main"
        return self.main

    def pull(self, n):
        return self.prs.get(n)

    def comments(self, n):
        return list(self.said[n])

    def check_runs(self, sha, *, all_runs=False):
        if all_runs:
            return list(self.history[sha])
        if sha in self.checks:
            return list(self.checks[sha])
        return list(GREEN) if any(p.head_sha == sha for p in self.prs.values()) else []

    def commit_statuses(self, sha):
        return {}

    def behind_by(self, base, sha):
        return next((self.behind.get(p.number, 0) for p in self.prs.values() if p.head_sha == sha), 0)

    def commit_time(self, sha):
        return 10_000.0 - self.tip_age

    def ref_sha(self, branch):
        assert branch == TRAIN_REF
        return self.train

    def git_commit(self, sha):
        return self.commits[sha]

    def tree_sha(self, sha):
        return self.commits[sha].tree

    def pulls_for_commit(self, sha):
        return list(self.assoc.get(sha, []))

    def pull_files(self, n):
        return list(self.files.get(n, ["src/app.ts"]))

    def e2e_runs(self, sha):
        return list(self.runs.get(sha, []))

    def annotations(self, check_id):
        return list(self.ann.get(check_id, []))

    # writes
    def update_branch(self, n, sha):  # the per-PR route: must never be called in batch mode
        self.writes.append(("update_branch", n, sha))
        return True

    def set_train_ref(self, sha, ref=TRAIN_REF):
        assert ref == TRAIN_REF
        self.writes.append(("update_ref", ref, sha))
        self.train = sha

    def merge_into_train(self, head_sha, message, base=TRAIN_REF):
        assert base == TRAIN_REF
        self.writes.append(("merge", base, head_sha, message))
        number = next(p.number for p in self.prs.values() if p.head_sha == head_sha)
        if number in self.conflicts:
            return MergeResult("conflict")
        parent = self.train
        sha = hashlib.sha1(f"{parent}+{head_sha}+{next(self._ids)}".encode()).hexdigest()
        self.commits[sha] = GitCommit(sha, message, tree_of(self.commits[parent].tree, head_sha), (parent, head_sha))
        self.train = sha
        return MergeResult("merged", sha)

    def dispatch_e2e(self, workflow=E2E_WORKFLOW, ref=TRAIN_REF):
        assert (workflow, ref) == (E2E_WORKFLOW, TRAIN_REF)
        self.writes.append(("dispatch", workflow, ref, self.train))
        run_id = next(self._ids)  # GitHub queues the run on the tip
        self.runs[self.train] = [
            WorkflowRun(run_id, "queued", "", 1, TRAIN_REF, self.train, f"https://x/runs/{run_id}")
        ]

    def rerun_failed(self, run_id):
        run_ = next(r for runs in self.runs.values() for r in runs if r.id == run_id)
        assert run_.head_branch == TRAIN_REF
        self.writes.append(("rerun", run_id))

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

    # what the world does between runs
    def e2e_result(self, conclusion, *, attempt=1, tip=None, tests=()):
        tip = tip or self.train
        run_id = self.runs[tip][0].id if tip in self.runs else next(self._ids)
        check_id = next(self._ids)
        status = "completed" if conclusion else "in_progress"
        self.runs[tip] = [WorkflowRun(run_id, status, conclusion, attempt, TRAIN_REF, tip, f"https://x/runs/{run_id}")]
        check = CheckRun(E2E, status, conclusion, f"2026-11-17T1{attempt}:00:00Z", check_id)
        self.checks[tip] = [check]
        self.history[tip].append(check)
        self.ann[check_id] = list(tests)

    def merge_pr(self, n, *, tree=None):
        """Ahmed squash-merges a pull request into main."""
        p = self.prs[n]
        sha = hashlib.sha1(f"main+{n}+{self.main}".encode()).hexdigest()
        self.commits[sha] = GitCommit(
            sha, f"#{n}", tree or tree_of(self.commits[self.main].tree, p.head_sha), (self.main,)
        )
        self.assoc[sha] = [n]
        self.main = sha
        self.prs[n] = PullRequest(**{**p.__dict__, "merged": True, "state": "closed"})

    def outsider_commit(self):
        sha = hashlib.sha1(f"hand+{self.main}".encode()).hexdigest()
        self.commits[sha] = GitCommit(sha, "hand", tree_of(self.commits[self.main].tree, "hand"), (self.main,))
        self.assoc[sha] = [999]
        self.main = sha

    def set(self, n, **kw):
        self.prs[n] = PullRequest(**{**self.prs[n].__dict__, **kw})

    def manifest(self):
        chain, sha = [], self.train
        while sha and sha != BASE and len(chain) <= MAX_BATCH:
            chain.append(self.commits[sha])
            sha = self.commits[sha].parents[0]
        if sha:
            chain.append(self.commits[sha])
        return parse_manifest(chain)

    def comments_of(self, n, kind):
        return [b for b in self.said[n] if f"<!-- merge-train:{kind}:" in b]


ALLOWED = {"update_ref", "merge", "dispatch", "rerun", "add_label", "remove_label", "comment"}


def go(gh, order, *, size=3, wave="w", dry_run=False, conflict_files=lambda base, head_sha: None):
    cfg = parse(json.dumps({"wave": wave, "order": order}))
    report = run(
        gh,
        cfg,
        options=Options(batch_size=size, dry_run=dry_run),
        conflict_files=conflict_files,
        now=lambda: 10_000.0,
        sleep=lambda s: None,
    )
    # D9: what batch mode may write, after every run
    for w in gh.writes:
        assert w[0] in ALLOWED, w
        if w[0] == "update_ref":
            assert w[1] == TRAIN_REF
        if w[0] == "merge":
            assert w[1] == TRAIN_REF
        if w[0] == "dispatch":
            assert w[1:3] == (E2E_WORKFLOW, TRAIN_REF)
    return report


def since(gh, mark):
    return gh.writes[mark:]


def kinds(writes):
    return [w[0] for w in writes]


def green_batch(n=3, order=None):
    """A built batch of the first n PRs whose E2E is green, ready announced."""
    order = order or list(range(1, n + 1))
    gh = BatchGH([pr(i) for i in order])
    go(gh, order)
    gh.e2e_result("success")
    go(gh, order)
    return gh, order


# --- options and command line --------------------------------------------------------------------------


def test_batch_size_is_0_to_4_default_3():
    assert Options().batch_size == 3
    for bad in (-1, 5, True):
        with pytest.raises(ValueError, match="batch-size"):
            Options(batch_size=bad)


def test_cli_accepts_batch_size_and_refuses_outside_0_to_4(monkeypatch, capsys):
    seen = {}

    def fake_run(gh, config, *, options, conflict_files):
        seen["size"] = options.batch_size
        from merge_train.train import Report

        return Report()

    class Stub:
        def default_branch(self):
            return "main"

        def read_file(self, path, ref):
            return '{"order": []}'

    monkeypatch.setattr("merge_train.__main__.GitHub", lambda repo, r, w: Stub())
    monkeypatch.setattr("merge_train.__main__.run", fake_run)
    monkeypatch.setenv("GH_READ_TOKEN", "r")
    monkeypatch.setenv("GH_WRITE_TOKEN", "w")
    assert cli(["--repo", "o/r"]) == 0 and seen["size"] == 3
    for n in "01234":
        assert cli(["--repo", "o/r", "--batch-size", n]) == 0 and seen["size"] == int(n)
    for bad in ("5", "-1", "x"):
        with pytest.raises(SystemExit):
            cli(["--repo", "o/r", "--batch-size", bad])
    # the flags Basel's reusable workflow passes (playbook ed7ad21)
    args = ["--repo", "o/r", "--repo-dir", "repo", "--config-path", ".github/merge-train.json"]
    args += ["--settle-seconds", "120", "--batch-size", "3", "--summary", "/dev/null", "--dry-run"]
    assert cli(args) == 0
    with pytest.raises(SystemExit):
        cli(["--repo", "o/r", "--train-ref", "main"])  # the ref is never an input


# --- the manifest --------------------------------------------------------------------------------------


def test_the_manifest_message_has_the_designed_format_and_reads_back():
    m = Member(226, head(226))
    assert manifest_message("0.2.2-w1", 4, m) == f"train 0.2.2-w1#4: #226@{head(226)}"
    assert wave_token("0.2.2 w#1 -->") == "0.2.2_w_1_--_"
    gh = BatchGH([pr(1), pr(2)])
    go(gh, [1, 2], wave="0.2.2-w1")
    got = gh.manifest()
    assert (got.wave, got.batch, got.base) == ("0.2.2-w1", 1, BASE)
    assert got.members == (Member(1, head(1)), Member(2, head(2)))
    assert got.tip == gh.train and not got.bisect_of


def test_a_tip_that_is_not_a_train_merge_has_no_manifest():
    assert parse_manifest([GitCommit(BASE, "feat: x", "t", ())]) is None
    assert parse_manifest([]) is None


# --- D1: who joins a batch -----------------------------------------------------------------------------


def facts_for(prs, **over):
    out = {}
    for p in prs:
        out[p.number] = Facts(p.number, p, "ok", "ok")
    for n, f in over.items():
        out[int(n[1:])] = f
    return out


def test_candidacy_reasons():
    ok = pr(1)
    assert candidacy(Facts(1, ok, "ok", "ok"), "main") is None
    cases = [
        (Facts(1, None), "not found"),
        (Facts(1, pr(1, merged=True, state="closed")), "merged"),
        (Facts(1, pr(1, state="closed")), "closed"),
        (Facts(1, pr(1, draft=True), "ok", "ok"), "draft"),
        (Facts(1, pr(1, base_ref="malek/x"), "ok", "ok"), "stacked on malek/x"),
        (Facts(1, pr(1, labels=frozenset()), "ok", "ok"), PLAIN),
        (Facts(1, ok, "failed", "ok"), "CI failed"),
        (Facts(1, ok, "ok", "pending"), "Owner review pending"),
        (Facts(1, ok, "ok", "ok", red_here=True), f"{RED_LABEL} on this head"),
        (Facts(1, ok, "ok", "ok", conflict_here=True), "conflicts in the train"),
    ]
    for f, reason in cases:
        assert candidacy(f, "main").startswith(reason)


@pytest.mark.parametrize("size", [1, 2, 3, 4])
def test_a_batch_is_the_first_k_candidates_and_a_non_candidate_does_not_block(size):
    prs = [pr(1, draft=True), pr(2), pr(3, labels=frozenset()), pr(4), pr(5), pr(6), pr(7)]
    facts = facts_for(prs)
    facts[5] = Facts(5, prs[4], "failed", "ok")
    sel = select_batch([1, 2, 3, 4, 5, 6, 7, 8], {**facts, 8: Facts(8, None)}, "main", size, lambda n: False)
    assert [p.number for p in sel.batch] == [2, 4, 6, 7][:size]
    assert dict(sel.skipped) == {1: "draft", 3: PLAIN, 5: "CI failed", 8: "not found"}
    assert [n for n, _ in sel.waiting] == [2, 4, 6, 7][size:]


# --- D3: a workflow change goes alone ------------------------------------------------------------------


def test_a_workflow_change_first_in_line_is_a_batch_of_one():
    prs = [pr(1), pr(2), pr(3)]
    asked = []
    sel = select_batch([1, 2, 3], facts_for(prs), "main", 3, lambda n: asked.append(n) or n == 1)
    assert [p.number for p in sel.batch] == [1] and asked == [1]
    assert [n for n, _ in sel.waiting] == [2, 3]


def test_a_workflow_change_later_ends_the_batch_before_it():
    prs = [pr(1), pr(2), pr(3), pr(4)]
    sel = select_batch([1, 2, 3, 4], facts_for(prs), "main", 4, lambda n: n == 3)
    assert [p.number for p in sel.batch] == [1, 2]
    assert sel.waiting[0] == (3, "changes .github/workflows: goes alone in its own batch")


def test_the_run_reads_pull_files_to_spot_a_workflow_change():
    gh = BatchGH([pr(1), pr(2)], files={1: ["README.md", ".github/workflows/e2e.yml"]})
    go(gh, [1, 2])
    assert [w[2] for w in gh.writes if w[0] == "merge"] == [head(1)]


# --- D2: building the batch -----------------------------------------------------------------------------


def test_the_first_run_builds_train_run_from_main_and_dispatches_e2e_once():
    gh = BatchGH([pr(1), pr(2), pr(3), pr(4)])
    report = go(gh, [1, 2, 3, 4])
    assert kinds(gh.writes) == ["update_ref", "merge", "merge", "merge", "dispatch"]
    assert gh.writes[0] == ("update_ref", TRAIN_REF, BASE)
    assert [w[3] for w in gh.writes[1:4]] == [f"train w#1: #{n}@{head(n)}" for n in (1, 2, 3)]
    assert gh.writes[-1][3] == gh.train
    assert "update_branch" not in kinds(gh.writes)
    rows = {n: s for n, s, _ in report.rows}
    assert rows[1] == "batch w#1 1/3" and rows[4] == "waiting"


def test_a_conflicting_member_leaves_the_batch_with_a_comment_and_the_label_and_the_rest_go_on():
    gh = BatchGH([pr(1), pr(2), pr(3)], conflicts={2})
    seen = []
    go(gh, [1, 2, 3], conflict_files=lambda base, h: seen.append((base, h)) or ["src/feed.ts"])
    assert [m.number for m in gh.manifest().members] == [1, 3]
    assert ("add_label", 2, CONFLICT_LABEL) in gh.writes
    body = gh.comments_of(2, "conflict")[0]
    assert marker("conflict", f"{head(2)[:7]}-{BASE[:7]}") in body and "`src/feed.ts`" in body and "Malek" in body
    assert seen[0][1] == head(2)
    assert kinds(gh.writes)[-1] == "dispatch"

    mark = len(gh.writes)
    go(gh, [1, 2, 3])  # E2E not visible yet: nothing new, the conflict is not reported twice
    assert since(gh, mark) == []


def test_a_conflict_with_an_earlier_member_only_says_so():
    gh = BatchGH([pr(1), pr(2)], conflicts={2})
    go(gh, [1, 2], conflict_files=lambda base, h: None if base != BASE else [])
    assert "التعارض مع الأعضاء اللي قبله" in gh.comments_of(2, "conflict")[0]


def test_when_every_member_conflicts_nothing_is_dispatched():
    gh = BatchGH([pr(1)], conflicts={1})
    go(gh, [1])
    assert "dispatch" not in kinds(gh.writes)


def test_a_conflicted_head_is_not_retried_on_the_same_main_but_is_after_main_moves():
    gh = BatchGH([pr(1), pr(2), pr(3)], conflicts={2})
    go(gh, [1, 2, 3])
    gh.e2e_result("success")
    go(gh, [1, 2, 3])
    gh.merge_pr(1)
    gh.merge_pr(3)
    gh.conflicts.clear()
    mark = len(gh.writes)
    go(gh, [1, 2, 3])  # batch done; main moved, so #2 is tried again
    assert [w[2] for w in since(gh, mark) if w[0] == "merge"] == [head(2)]
    assert ("remove_label", 2, CONFLICT_LABEL) in since(gh, mark)
    assert gh.manifest().batch == 2


# --- D4: E2E on the batch ------------------------------------------------------------------------------


def test_e2e_running_means_wait_and_missing_means_one_dispatch_after_the_settle_time():
    gh = BatchGH([pr(1)])
    go(gh, [1])
    del gh.runs[gh.train]  # the dispatched run does not show yet
    mark = len(gh.writes)
    gh.tip_age = 30.0
    go(gh, [1])
    assert since(gh, mark) == []
    gh.tip_age = 600.0
    go(gh, [1])
    assert kinds(since(gh, mark)) == ["dispatch"]
    gh.e2e_result("")  # running
    mark = len(gh.writes)
    go(gh, [1])
    assert since(gh, mark) == []


def test_an_e2e_run_with_no_e2e_check_is_reported_not_retried():
    gh = BatchGH([pr(1)])
    go(gh, [1])
    gh.e2e_result("success")
    gh.checks[gh.train] = []
    mark = len(gh.writes)
    report = go(gh, [1])
    assert since(gh, mark) == [] and any("ATTENTION" in a for a in report.actions)


# --- D5: green batch, order-gated ready ------------------------------------------------------------------


def test_a_green_batch_announces_each_member_in_order_once():
    gh, order = green_batch(3)
    texts = [gh.comments_of(n, "ready")[0] for n in order]
    assert "جاهز للدمج (batch w#1, 1/3): الأول في الـ batch" in texts[0]
    assert "جاهز للدمج (batch w#1, 2/3): بعد #1" in texts[1]
    assert "جاهز للدمج (batch w#1, 3/3): بعد #2" in texts[2]
    tip = gh.train
    for n, text in zip(order, texts, strict=True):
        assert marker("ready", f"{head(n)}@w#1:{tip[:12]}") in text
        assert "الإعلان يشمل CI وE2E وOwner review فقط" in text and "#1 ثم #2 ثم #3" in text
    mark = len(gh.writes)
    go(gh, order)
    assert since(gh, mark) == [], "announced once"


def test_merging_in_order_keeps_the_batch_and_adds_nothing():
    gh, order = green_batch(3)
    gh.merge_pr(1)
    mark = len(gh.writes)
    go(gh, order)
    assert since(gh, mark) == []
    gh.merge_pr(2)
    go(gh, order)
    assert since(gh, mark) == []


def test_a_check_running_again_holds_the_ready_signal_without_dropping_the_batch():
    gh = BatchGH([pr(1), pr(2)])
    go(gh, [1, 2])
    gh.e2e_result("success")
    gh.checks[head(2)] = [GREEN[0], CheckRun("Owner review", "in_progress", "", "2026-11-17T11:00:00Z", 9)]
    mark = len(gh.writes)
    report = go(gh, [1, 2])
    assert since(gh, mark) == [] and any("held" in a for a in report.actions)
    del gh.checks[head(2)]
    go(gh, [1, 2])
    assert len(gh.comments_of(1, "ready")) == len(gh.comments_of(2, "ready")) == 1


# --- D6: every invalidation reason ---------------------------------------------------------------------


def push(gh, order):
    gh.set(2, head_sha=head(2, 1))


def close(gh, order):
    gh.set(2, state="closed")


def draft(gh, order):
    gh.set(2, draft=True)


def ci_red(gh, order):
    gh.checks[head(2)] = [CheckRun("ci / Build and test", "completed", "failure", "t", 1), GREEN[1]]


def owner_red(gh, order):
    gh.checks[head(2)] = [GREEN[0], CheckRun("Owner review", "completed", "failure", "t", 2)]


def outsider(gh, order):
    gh.outsider_commit()


def out_of_order(gh, order):
    gh.merge_pr(2)


def reorder(gh, order):
    order[:] = [2, 1, 3]


def removed(gh, order):
    order.remove(2)


@pytest.mark.parametrize(
    "change, reason",
    [
        (push, "اتعمل push على #2"),
        (close, "#2 اتقفل"),
        (draft, "#2 بقى draft"),
        (ci_red, "CI على #2 مبقاش أخضر"),
        (owner_red, "Owner review على #2 مبقاش أخضر"),
        (outsider, "main اتحرك بـ commit مش العضو التالي في الـ batch"),
        (out_of_order, "#2 اتدمج قبل دوره"),
        (reorder, "ترتيب الأعضاء في الترتيب المنشور اتغيّر"),
        (removed, "#2 اتشال من الترتيب المنشور"),
    ],
)
def test_an_invalid_batch_withdraws_its_ready_comments_and_is_rebuilt(change, reason):
    gh, order = green_batch(3)
    old_tip = gh.train
    change(gh, order)
    mark = len(gh.writes)
    report = go(gh, order)
    assert any(f"dropped: {reason}" in a for a in report.actions), report.actions
    for n in (1, 3):
        tag = marker("ready", f"{head(n)}@w#1:{old_tip[:12]}:withdrawn")
        withdrawn = [b for b in gh.said[n] if tag in b]
        assert withdrawn and f"سُحب: batch اتلغى ({reason})" in withdrawn[0]
    assert "update_ref" in kinds(since(gh, mark)), "the next run rebuilds from the current state"
    assert gh.manifest().batch == 2


def test_a_new_wave_drops_the_live_batch():
    gh, order = green_batch(2)
    report = go(gh, order, wave="w2")
    assert any("الـ wave اتغيّرت" in a for a in report.actions)
    assert gh.manifest().wave == "w2" and gh.manifest().batch == 1


def test_main_moving_by_the_next_member_in_order_is_not_an_invalidation():
    members = (Member(1, head(1)), Member(2, head(2)))
    path = [MainStep("m1", frozenset({1})), MainStep("m2", frozenset({2}))]
    assert main_moved_by(path, members) == (2, 1, True)
    assert main_moved_by(path[1:], members) == (0, -1, False)
    rebase = [MainStep("a", frozenset({1})), MainStep("b", frozenset({1})), MainStep("c", frozenset({7}))]
    assert main_moved_by(rebase, members) == (1, 1, False)


def test_assess_holds_on_a_running_check_and_drops_on_a_missing_one():
    m = parse_manifest(
        [
            GitCommit("t2", f"train w#1: #2@{head(2)}", "x", ("t1", head(2))),
            GitCommit("t1", f"train w#1: #1@{head(1)}", "x", (BASE, head(1))),
        ]
    )
    pulls = {1: pr(1), 2: pr(2)}
    args = ([1, 2], "w", "main", ())
    assert assess(m, m.members, pulls, {1: ("ok", "ok"), 2: ("ok", "pending")}, *args).pending == (2,)
    assert assess(m, m.members, pulls, {1: ("ok", "ok"), 2: ("missing", "ok")}, *args).reason
    assert assess(m, m.members, pulls, {1: ("ok", "ok"), 2: ("ok", "ok")}, [1, 2], "w", "main", None).reason


# --- D7: red batch, one re-run, prefix bisect ------------------------------------------------------------


def drive(k: int, culprit: int | None, flaky: bool = False):
    """Run next_step against a world where a build is red iff it contains the culprit."""
    prefix, attempt, extra = None, 1, 0
    while True:
        size = k if prefix is None else prefix
        red = culprit is not None and culprit < size
        if prefix is None and flaky and attempt == 2:
            red = False
        step = next_step(k, prefix, "failed" if red else "ok", attempt)
        if step.kind == "rerun":
            attempt = 2
        elif step.kind == "bisect":
            prefix, extra = step.prefix, extra + 1
        else:
            assert step.kind == "settled"
            return step, extra


@pytest.mark.parametrize("k", [1, 2, 3, 4])
def test_the_prefix_bisect_finds_the_culprit_in_at_most_k_minus_1_runs(k):
    for culprit in range(k):
        step, extra = drive(k, culprit)
        assert (step.culprit, step.green) == (culprit, culprit)
        assert extra <= max(k - 1, 0)
    step, extra = drive(k, None)
    assert (step.culprit, step.green, step.flaky, extra) == (None, k, False, 0)
    step, extra = drive(k, 0, flaky=True)
    assert (step.culprit, step.green, step.flaky, extra) == (None, k, True, 0)


def test_next_step_waits_dispatches_and_reports():
    assert next_step(3, None, "pending", 1).kind == "wait"
    assert next_step(3, None, "missing", 1).kind == "dispatch"
    assert next_step(3, 1, "stuck", 1).kind == "stuck"


def test_a_red_batch_is_rerun_once_then_bisected_and_the_culprit_is_named():
    gh = BatchGH([pr(1), pr(2), pr(3)])
    order = [1, 2, 3]
    go(gh, order)
    full_tip = gh.train
    gh.e2e_result("failure", tests=["feed › plan total"])
    mark = len(gh.writes)
    go(gh, order)
    assert kinds(since(gh, mark)) == ["rerun"]

    gh.e2e_result("failure", attempt=2, tests=["feed › plan total"])
    mark = len(gh.writes)
    go(gh, order)
    assert kinds(since(gh, mark)) == ["update_ref", "merge", "dispatch"], "prefix [1]"
    message = since(gh, mark)[1][3]
    assert message.startswith(f"train w#1: #1@{head(1)}\n\nTrain-Bisect: {full_tip} #1@")
    assert gh.manifest().bisect_of == tuple(Member(n, head(n)) for n in order)

    gh.e2e_result("success")  # [1] green
    mark = len(gh.writes)
    go(gh, order)
    assert kinds(since(gh, mark)) == ["update_ref", "merge", "merge", "dispatch"], "prefix [1, 2]"

    gh.e2e_result("failure", tests=["stock › leftovers"])  # [1, 2] red: #2 is the culprit
    mark = len(gh.writes)
    go(gh, order)
    red = gh.comments_of(2, "red")
    assert len(red) == 1 and "stock › leftovers" in red[0] and "https://x/runs/" in red[0]
    assert ("add_label", 2, RED_LABEL) in gh.writes
    ready = gh.comments_of(1, "ready")
    assert len(ready) == 1 and "(batch w#1, 1/1)" in ready[0]
    assert not gh.comments_of(3, "ready"), "after the culprit: back to the queue"

    mark = len(gh.writes)
    go(gh, order)
    assert since(gh, mark) == [], "idempotent"

    gh.merge_pr(1)
    go(gh, order)  # done; next batch: #3 only, #2 stays out while its head is the red one
    assert [m.number for m in gh.manifest().members] == [3] and gh.manifest().batch == 2


@pytest.mark.parametrize("k", [1, 2, 3, 4])
@pytest.mark.parametrize("where", ["first", "last"])
def test_bisect_end_to_end_for_each_batch_size(k, where):
    order = list(range(1, k + 1))
    culprit = 1 if where == "first" else k
    gh = BatchGH([pr(n) for n in order])
    go(gh, order, size=k)
    gh.e2e_result("failure")
    go(gh, order, size=k)  # re-run
    gh.e2e_result("failure", attempt=2)
    runs = 0
    for _ in range(k + 2):
        go(gh, order, size=k)
        if gh.comments_of(culprit, "red"):
            break
        runs += 1
        m = gh.manifest()
        gh.e2e_result("failure" if any(x.number == culprit for x in m.members) else "success")
    assert gh.comments_of(culprit, "red"), "the culprit is named"
    assert runs <= k - 1
    for n in order:
        assert bool(gh.comments_of(n, "ready")) == (n < culprit)
    assert sum(1 for n in order if gh.comments_of(n, "red")) == 1


def test_green_on_the_rerun_is_flaky_with_the_test_names_and_the_batch_goes_on():
    gh = BatchGH([pr(1), pr(2)])
    go(gh, [1, 2])
    gh.e2e_result("failure", tests=["login › works"])
    go(gh, [1, 2])
    gh.e2e_result("success", attempt=2)
    report = go(gh, [1, 2])
    flaky = gh.comments_of(1, "flaky")
    assert len(flaky) == 1 and "login › works" in flaky[0] and "Salma" in flaky[0]
    assert all("flaky" in gh.comments_of(n, "ready")[0] for n in (1, 2))
    assert any("flaky" in a for a in report.actions)


# --- D8: the tree check ---------------------------------------------------------------------------------


def test_equal_trees_finish_the_batch_quietly_and_the_next_batch_starts():
    gh = BatchGH([pr(1), pr(2), pr(3)])
    go(gh, [1, 2, 3], size=2)
    gh.e2e_result("success")
    go(gh, [1, 2, 3], size=2)
    gh.merge_pr(1)
    gh.merge_pr(2)
    mark = len(gh.writes)
    report = go(gh, [1, 2, 3], size=2)
    assert any("tree check: main at" in a and "has the tree" in a for a in report.actions)
    assert kinds(since(gh, mark)) == ["update_ref", "merge", "dispatch"]
    assert since(gh, mark)[0][2] == gh.main, "the next batch starts from the new main"


def test_different_trees_are_reported_and_nothing_is_dispatched_on_main():
    gh = BatchGH([pr(1), pr(2)])
    go(gh, [1, 2], size=2)
    gh.e2e_result("success")
    go(gh, [1, 2], size=2)
    gh.merge_pr(1)
    gh.merge_pr(2, tree="hand-edited-tree")
    mark = len(gh.writes)
    report = go(gh, [1, 2], size=2)
    tree = gh.comments_of(2, "tree")
    assert len(tree) == 1 and "nightly" in tree[0] and "يدويا" in tree[0]
    assert "dispatch" not in kinds(since(gh, mark)), "the train never dispatches on main"
    assert any("ATTENTION: tree check" in a for a in report.actions)
    go(gh, [1, 2], size=2)
    assert len(gh.comments_of(2, "tree")) == 1


# --- pull requests without run-e2e, dry run --------------------------------------------------------------


def test_a_pull_request_without_run_e2e_gets_the_plain_ready_check_in_order():
    gh = BatchGH([pr(1, labels=frozenset()), pr(2), pr(3, labels=frozenset())])
    report = go(gh, [1, 2, 3])
    first = gh.comments_of(1, "ready")
    assert len(first) == 1 and "جاهز للدمج: #1" in first[0] and "يلغي الـ batch" in first[0]
    assert not gh.comments_of(3, "ready"), "waits behind the live batch member #2"
    assert {n: s for n, s, _ in report.rows}[3] == "waiting"


def test_a_plain_pull_request_behind_main_is_not_announced_and_never_updated():
    gh = BatchGH([pr(1, labels=frozenset())])
    gh.behind[1] = 2
    go(gh, [1])
    assert gh.writes == []


def test_dry_run_writes_nothing():
    gh = BatchGH([pr(1), pr(2)], conflicts={2})
    report = go(gh, [1, 2], dry_run=True)
    assert gh.writes == [] and any("dry run: would build batch w#1" in a for a in report.actions)


def test_an_empty_order_with_no_batch_does_nothing():
    gh = BatchGH([])
    report = go(gh, [])
    assert gh.writes == [] and "empty" in report.actions[0]
