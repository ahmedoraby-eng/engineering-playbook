"""D9 and Yousef's record 5d9d0b13: the batch-mode writes of the GitHub client are fenced to train/run.

`actions: write` could dispatch, cancel or re-run any workflow, and `contents: write` could move any ref,
so the client itself refuses anything else, before any HTTP call."""

from __future__ import annotations

import io
import json
import re
import urllib.error

import pytest

from merge_train.github import E2E_WORKFLOW, TRAIN_REF, GitHub, GitHubError, MergeResult, RefusedWrite

SHA = "a" * 40
TIP = "e" * 40


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
    """`handler(method, path, body)` answers with a Response or an int (an HTTP error status)."""
    calls = []

    def transport(request):
        path = request.full_url.removeprefix("https://api.github.com")
        body = json.loads(request.data) if request.data else None
        calls.append((request.get_method(), path, request.get_header("Authorization"), body))
        out = handler(request.get_method(), path, body)
        if isinstance(out, int):
            raise urllib.error.HTTPError(request.full_url, out, "x", {}, io.BytesIO(b'{"message": "no"}'))
        return out

    return GitHub("o/r", "READ", "WRITE", transport=transport, sleep=lambda s: None), calls


def writes(calls):
    return [(m, p, b) for m, p, _, b in calls if m != "GET"]


def test_the_constants_are_what_the_design_says():
    assert TRAIN_REF == "train/run" and E2E_WORKFLOW == "e2e.yml"


# --- update-ref: train/run only -------------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["main", "refs/heads/main", "train/run2", "train", "", "heads/train/run"])
def test_update_ref_refuses_any_other_ref_before_calling(ref):
    gh, calls = client(lambda m, p, b: Response(200, {}))
    with pytest.raises(RefusedWrite):
        gh.set_train_ref(SHA, ref=ref)
    assert calls == []


@pytest.mark.parametrize("sha", ["", "main", "refs/heads/main", "A" * 40, "../x"])
def test_update_ref_refuses_anything_but_a_sha(sha):
    gh, calls = client(lambda m, p, b: Response(200, {}))
    with pytest.raises(RefusedWrite):
        gh.set_train_ref(sha)
    assert calls == []


def test_update_ref_forces_train_run_and_creates_it_the_first_time():
    gh, calls = client(lambda m, p, b: Response(200, {}))
    gh.set_train_ref(SHA)
    assert writes(calls) == [("PATCH", "/repos/o/r/git/refs/heads/train/run", {"sha": SHA, "force": True})]
    assert calls[0][2] == "Bearer WRITE"

    answers = iter([422, Response(201, {})])
    gh, calls = client(lambda m, p, b: next(answers))
    gh.set_train_ref(SHA)
    assert writes(calls)[1] == ("POST", "/repos/o/r/git/refs", {"ref": "refs/heads/train/run", "sha": SHA})

    gh, _ = client(lambda m, p, b: 403)
    with pytest.raises(GitHubError, match="403"):
        gh.set_train_ref(SHA)


# --- merges: base train/run only ------------------------------------------------------------------------


@pytest.mark.parametrize("base", ["main", "refs/heads/main", "master", ""])
def test_merge_refuses_any_base_but_train_run_before_calling(base):
    gh, calls = client(lambda m, p, b: Response(201, {"sha": TIP}))
    with pytest.raises(RefusedWrite):
        gh.merge_into_train(SHA, "train w#1: #1@" + SHA, base=base)
    assert calls == []


def test_merge_into_train_outcomes():
    gh, calls = client(lambda m, p, b: Response(201, {"sha": TIP}))
    assert gh.merge_into_train(SHA, "msg") == MergeResult("merged", TIP)
    assert writes(calls) == [("POST", "/repos/o/r/merges", {"base": "train/run", "head": SHA, "commit_message": "msg"})]
    assert calls[0][2] == "Bearer WRITE"
    gh, _ = client(lambda m, p, b: Response(204, None))
    assert gh.merge_into_train(SHA, "msg") == MergeResult("noop")
    gh, _ = client(lambda m, p, b: 409)
    assert gh.merge_into_train(SHA, "msg") == MergeResult("conflict")
    for status in (404, 403, 422):
        gh, _ = client(lambda m, p, b, s=status: s)
        with pytest.raises(GitHubError, match=str(status)):
            gh.merge_into_train(SHA, "msg")


# --- dispatch: e2e.yml on train/run only ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"ref": "main"},
        {"ref": "refs/heads/train/run"},
        {"workflow": "ci.yml"},
        {"workflow": "merge-train.yml"},
        {"workflow": "e2e.yml", "ref": "main"},
    ],
)
def test_dispatch_refuses_another_workflow_or_ref_before_calling(kw):
    gh, calls = client(lambda m, p, b: Response(204, None))
    with pytest.raises(RefusedWrite):
        gh.dispatch_e2e(**kw)
    assert calls == []


def test_dispatch_starts_e2e_on_train_run():
    gh, calls = client(lambda m, p, b: Response(204, None))
    gh.dispatch_e2e()
    assert writes(calls) == [("POST", "/repos/o/r/actions/workflows/e2e.yml/dispatches", {"ref": "train/run"})]
    assert calls[0][2] == "Bearer WRITE"


# --- re-run: only a run of e2e.yml on train/run ----------------------------------------------------------


def run_payload(run_id, branch):
    return {
        "id": run_id,
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "head_branch": branch,
        "head_sha": TIP,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def actions_api(branch="train/run", listed=(77,)):
    def handler(method, path, body):
        if method == "GET" and path == "/repos/o/r/actions/runs/77":
            return Response(200, run_payload(77, branch))
        if method == "GET" and path.startswith("/repos/o/r/actions/workflows/e2e.yml/runs?"):
            return Response(200, {"workflow_runs": [run_payload(n, branch) for n in listed]})
        if method == "POST" and path == "/repos/o/r/actions/runs/77/rerun-failed-jobs":
            return Response(201, {})
        return 404

    return handler


def test_rerun_reads_the_run_first_and_reruns_only_on_train_run():
    gh, calls = client(actions_api())
    gh.rerun_failed(77)
    assert [c[0] for c in calls] == ["GET", "GET", "POST"], "the run is read before the write"
    assert calls[1][1].startswith("/repos/o/r/actions/workflows/e2e.yml/runs?branch=train%2Frun&head_sha=")
    assert writes(calls) == [("POST", "/repos/o/r/actions/runs/77/rerun-failed-jobs", None)]
    assert calls[-1][2] == "Bearer WRITE" and {c[2] for c in calls[:2]} == {"Bearer READ"}


@pytest.mark.parametrize("branch", ["main", "feature/x", "train/run2", ""])
def test_rerun_refuses_a_run_on_another_branch(branch):
    gh, calls = client(actions_api(branch=branch))
    with pytest.raises(RefusedWrite):
        gh.rerun_failed(77)
    assert writes(calls) == []


def test_rerun_refuses_a_run_of_another_workflow_on_train_run():
    gh, calls = client(actions_api(listed=(78,)))  # 77 is on train/run but is not an e2e.yml run
    with pytest.raises(RefusedWrite, match="not a e2e.yml run"):
        gh.rerun_failed(77)
    assert writes(calls) == []


# --- every write the client can make ---------------------------------------------------------------------


ALLOWED_WRITES = [
    ("PUT", r"/repos/o/r/pulls/\d+/update-branch"),
    ("POST", r"/repos/o/r/issues/\d+/labels"),
    ("DELETE", r"/repos/o/r/issues/\d+/labels/[^/]+"),
    ("POST", r"/repos/o/r/issues/\d+/comments"),
    ("PATCH", r"/repos/o/r/git/refs/heads/train/run"),
    ("POST", r"/repos/o/r/git/refs"),
    ("POST", r"/repos/o/r/merges"),
    ("POST", r"/repos/o/r/actions/workflows/e2e\.yml/dispatches"),
    ("POST", r"/repos/o/r/actions/runs/\d+/rerun-failed-jobs"),
]


def test_every_write_lands_on_an_allowed_path_with_train_run_as_its_only_ref():
    def handler(method, path, body):
        if method == "PATCH":
            return 422  # train/run not there yet: exercise the create path too
        if method == "GET":
            return actions_api()(method, path, body)
        if path.endswith("/dispatches"):
            return Response(204, None)
        return Response({"PUT": 202, "DELETE": 204}.get(method, 201), {"sha": TIP})

    gh, calls = client(handler)
    gh.update_branch(1, SHA)
    gh.add_label(1, "train-red")
    gh.remove_label(1, "train-red")
    gh.comment(1, "hi")
    gh.set_train_ref(SHA)
    gh.merge_into_train(SHA, "m")
    gh.dispatch_e2e()
    gh.rerun_failed(77)
    for method, path, body in writes(calls):
        assert any(method == m and re.fullmatch(p, path) for m, p in ALLOWED_WRITES), (method, path)
        if path.endswith("/git/refs"):
            assert body["ref"] == "refs/heads/train/run"
        if path.endswith("/merges"):
            assert body["base"] == "train/run"
        if path.endswith("/dispatches"):
            assert body == {"ref": "train/run"}
    assert all(a == "Bearer WRITE" for m, _, a, _ in calls if m != "GET")
    assert not any("cancel" in p or p.endswith("/merge") for _, p, _, _ in calls)


# --- the new reads ---------------------------------------------------------------------------------------


def test_batch_reads_are_parsed_with_the_read_token():
    def handler(method, path, body):
        if path == "/repos/o/r/git/ref/heads/train/run":
            return Response(200, {"object": {"sha": TIP}})
        if path == f"/repos/o/r/git/commits/{TIP}":
            return Response(200, {"sha": TIP, "message": "m", "tree": {"sha": "t1"}, "parents": [{"sha": SHA}]})
        if path.startswith(f"/repos/o/r/commits/{TIP}/pulls"):
            return Response(200, [{"number": 5}])
        if path.startswith("/repos/o/r/pulls/5/files"):
            return Response(200, [{"filename": "a.ts"}, {"filename": "b.yml", "previous_filename": ".github/x.yml"}])
        if path.startswith("/repos/o/r/actions/workflows/e2e.yml/runs?head_sha="):
            return Response(200, {"workflow_runs": [run_payload(3, "train/run"), run_payload(9, "train/run")]})
        if path.startswith("/repos/o/r/check-runs/4/annotations"):
            return Response(
                200,
                [
                    {"annotation_level": "failure", "title": "login › works", "message": "x"},
                    {"annotation_level": "warning", "title": "slow"},
                    {"annotation_level": "failure", "title": "", "message": "feed plan\nstack"},
                ],
            )
        if path.startswith(f"/repos/o/r/commits/{TIP}/check-runs?filter=all"):
            return Response(200, {"check_runs": []})
        return 404

    gh, calls = client(handler)
    assert gh.ref_sha(TRAIN_REF) == TIP
    commit = gh.git_commit(TIP)
    assert (commit.message, commit.tree, commit.parents) == ("m", "t1", (SHA,))
    assert gh.tree_sha(TIP) == "t1"
    assert gh.pulls_for_commit(TIP) == [5]
    assert gh.pull_files(5) == ["a.ts", "b.yml", ".github/x.yml"]
    assert [r.id for r in gh.e2e_runs(TIP)] == [9, 3], "newest first"
    assert gh.annotations(4) == ["login › works", "feed plan"]
    assert gh.check_runs(TIP, all_runs=True) == []
    assert {(m, a) for m, _, a, _ in calls} == {("GET", "Bearer READ")}
    gh, _ = client(lambda m, p, b: 404)
    assert gh.ref_sha(TRAIN_REF) is None
