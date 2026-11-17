"""The few GitHub REST calls the merge train makes, over the standard library.

Two tokens, by design (ADR-020 E1): reads use the workflow's `GITHUB_TOKEN` (read permissions), and
writes use the `basita-train` App's token, because a push or a label made with `GITHUB_TOKEN` does not
start other workflows. The App therefore needs only contents and pull-requests write.

The write methods are exactly: update a pull request's branch, add or remove a label, post a comment, and,
for batch mode (Yousef's amendment D2, D4, D7, D9): point `train/run` at a commit, merge a head into
`train/run`, dispatch `e2e.yml` on `train/run`, and re-run the failed jobs of an E2E run on `train/run`.
There is no method that merges into the default branch or writes any other ref, and no cancel call: only
Ahmed merges (AGENTS.md), and `actions: write` could otherwise dispatch, cancel or re-run any workflow.
Each batch write checks its ref, base, workflow and run **before** any HTTP call (Yousef, record 5d9d0b13).
`TRAIN_REF` and `E2E_WORKFLOW` are constants of this module, never inputs. A test pins all of this.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

API = "https://api.github.com"
PER_PAGE = 100
# Only comments written by the train's own App count as its markers (a human comment must not be able
# to suppress or fake «جاهز للدمج»). The REST API names a GitHub App's comments "<slug>[bot]".
TRAIN_LOGIN = "basita-train[bot]"
# The one branch the train writes (batch mode, D2). A constant, never an input: every write that names a
# ref or a merge base refuses anything else before it calls GitHub.
TRAIN_REF = "train/run"
# The one workflow the train may dispatch, and only on TRAIN_REF (D4; Yousef, record 5d9d0b13).
E2E_WORKFLOW = "e2e.yml"


class GitHubError(RuntimeError):
    """A call failed. The message never carries a token."""


class RefusedWrite(GitHubError):
    """A write outside the fence (another ref, base, workflow or run). Raised before any HTTP call."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str  # "open" or "closed"
    merged: bool
    draft: bool
    title: str
    base_ref: str
    head_ref: str
    head_sha: str
    labels: frozenset[str]


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued, in_progress, completed
    conclusion: str  # success, failure, skipped, ... or "" while running
    started_at: str
    id: int = 0


@dataclass(frozen=True)
class GitCommit:
    sha: str
    message: str
    tree: str
    parents: tuple[str, ...]


@dataclass(frozen=True)
class MergeResult:
    """`POST /merges` into train/run: merged (sha of the new merge commit), conflict (409), or noop (204:
    the head is already in train/run)."""

    status: str
    sha: str = ""


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    status: str  # queued, in_progress, completed, ...
    conclusion: str  # "" while running
    attempt: int
    head_branch: str
    head_sha: str
    html_url: str


Transport = Callable[[urllib.request.Request], Any]


def _open(request: urllib.request.Request) -> Any:
    return urllib.request.urlopen(request, timeout=30)  # noqa: S310 - https URL built from API


class GitHub:
    def __init__(
        self,
        repo: str,
        read_token: str,
        write_token: str,
        *,
        transport: Transport = _open,
        sleep: Callable[[float], None] = time.sleep,
        train_login: str = TRAIN_LOGIN,
    ) -> None:
        if not repo or repo.count("/") != 1:
            raise ValueError("repo must be 'owner/name'")
        self.repo = repo
        self._read_token = read_token
        self._write_token = write_token
        self._transport = transport
        self._sleep = sleep
        self._train_login = train_login

    # --- transport ------------------------------------------------------------------------------

    def _request(self, method: str, path: str, token: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "basita-merge-train",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
            try:
                with self._transport(request) as response:
                    raw = response.read()
                    return response.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                if exc.code in (429, 502, 503, 504) and attempt < 2:
                    self._sleep(2 * (attempt + 1))
                    continue
                try:
                    payload = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    payload = None
                return exc.code, payload
            except urllib.error.URLError as exc:
                if attempt < 2:
                    self._sleep(2 * (attempt + 1))
                    continue
                raise GitHubError(f"{method} {path}: {exc.reason}") from exc
        raise GitHubError(f"{method} {path}: gave up")  # pragma: no cover - the loop always returns or raises

    def _read(self, path: str) -> tuple[int, Any]:
        return self._request("GET", path, self._read_token)

    def _write(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        return self._request(method, path, self._write_token, body)

    @staticmethod
    def _message(payload: Any) -> str:
        return str(payload.get("message", "")) if isinstance(payload, dict) else ""

    def _ok(self, method: str, path: str, status: int, payload: Any, expected: tuple[int, ...]) -> Any:
        if status not in expected:
            raise GitHubError(f"{method} {path} -> {status} {self._message(payload)}".strip())
        return payload

    def _paged(self, path: str, key: str | None = None) -> list[Any]:
        joiner = "&" if "?" in path else "?"
        items: list[Any] = []
        for page in range(1, 11):
            full = f"{path}{joiner}per_page={PER_PAGE}&page={page}"
            status, payload = self._read(full)
            payload = self._ok("GET", full, status, payload, (200,))
            chunk = payload.get(key, []) if key else payload
            items.extend(chunk)
            if len(chunk) < PER_PAGE:
                break
        return items

    # --- reads (GITHUB_TOKEN) -------------------------------------------------------------------

    def default_branch(self) -> str:
        path = f"/repos/{self.repo}"
        status, payload = self._read(path)
        return str(self._ok("GET", path, status, payload, (200,))["default_branch"])

    def read_file(self, path: str, ref: str) -> str | None:
        """A file's text at `ref`, or None when the file is not there."""
        url = f"/repos/{self.repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref, safe='')}"
        status, payload = self._read(url)
        if status == 404:
            return None
        payload = self._ok("GET", url, status, payload, (200,))
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise GitHubError(f"{path} is not a plain file")
        return base64.b64decode(payload["content"]).decode("utf-8")

    def pull(self, number: int) -> PullRequest | None:
        path = f"/repos/{self.repo}/pulls/{number}"
        status, payload = self._read(path)
        if status == 404:
            return None
        p = self._ok("GET", path, status, payload, (200,))
        return PullRequest(
            number=number,
            state=p["state"],
            merged=bool(p.get("merged")),
            draft=bool(p.get("draft")),
            title=p.get("title", ""),
            base_ref=p["base"]["ref"],
            head_ref=p["head"]["ref"],
            head_sha=p["head"]["sha"],
            labels=frozenset(label["name"] for label in p.get("labels", [])),
        )

    def behind_by(self, base: str, head_sha: str) -> int:
        """How many commits `base` has that `head_sha` lacks. (`mergeable_state: behind` needs branch
        protection, which GitHub Free does not offer on a private repo, so this is the reliable test.)"""
        path = f"/repos/{self.repo}/compare/{urllib.parse.quote(base, safe='')}...{head_sha}"
        status, payload = self._read(path)
        return int(self._ok("GET", path, status, payload, (200,)).get("behind_by", 0))

    def branch_sha(self, branch: str) -> str:
        path = f"/repos/{self.repo}/branches/{urllib.parse.quote(branch, safe='')}"
        status, payload = self._read(path)
        return str(self._ok("GET", path, status, payload, (200,))["commit"]["sha"])

    def commit_time(self, sha: str) -> float:
        """When the commit was committed, as epoch seconds."""
        path = f"/repos/{self.repo}/commits/{sha}"
        status, payload = self._read(path)
        stamp = self._ok("GET", path, status, payload, (200,))["commit"]["committer"]["date"]
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()

    def comments(self, number: int) -> list[str]:
        """The bodies of the comments the train itself wrote (its markers live there); anyone else's
        comment, human or bot, is not read as a marker."""
        items = self._paged(f"/repos/{self.repo}/issues/{number}/comments")
        return [
            str(item.get("body", "")) for item in items if (item.get("user") or {}).get("login") == self._train_login
        ]

    def commit_statuses(self, sha: str) -> dict[str, str]:
        """The latest state of each commit status context (success, pending, failure, error). The check-runs
        API does not show these, e.g. `Jira status`. Needs the `statuses: read` permission."""
        path = f"/repos/{self.repo}/commits/{sha}/status"
        status, payload = self._read(path)
        payload = self._ok("GET", path, status, payload, (200,))
        return {str(item["context"]): str(item["state"]) for item in payload.get("statuses", [])}

    def check_runs(self, sha: str, *, all_runs: bool = False) -> list[CheckRun]:
        """The check runs on a commit. By default GitHub lists only the latest run of each check; `all_runs`
        also lists earlier attempts (the failed first attempt of a re-run E2E, D7)."""
        path = f"/repos/{self.repo}/commits/{sha}/check-runs" + ("?filter=all" if all_runs else "")
        items = self._paged(path, "check_runs")
        return [
            CheckRun(
                name=item["name"],
                status=item["status"],
                conclusion=item.get("conclusion") or "",
                started_at=item.get("started_at") or "",
                id=int(item.get("id") or 0),
            )
            for item in items
        ]

    def annotations(self, check_run_id: int) -> list[str]:
        """The failure annotations of a check run, one line each (Playwright names the failing test there)."""
        items = self._paged(f"/repos/{self.repo}/check-runs/{int(check_run_id)}/annotations")
        lines = []
        for item in items:
            if item.get("annotation_level") != "failure":
                continue
            text = str(item.get("title") or "") or str(item.get("message") or "").strip().split("\n")[0]
            if text:
                lines.append(text)
        return lines

    def ref_sha(self, branch: str) -> str | None:
        """The commit a branch points at, or None when the branch does not exist (train/run before its
        first batch)."""
        path = f"/repos/{self.repo}/git/ref/heads/{urllib.parse.quote(branch, safe='/')}"
        status, payload = self._read(path)
        if status == 404:
            return None
        return str(self._ok("GET", path, status, payload, (200,))["object"]["sha"])

    def git_commit(self, sha: str) -> GitCommit:
        """A commit's message, tree and parents (the manifest on train/run is read this way, D2)."""
        path = f"/repos/{self.repo}/git/commits/{sha}"
        status, payload = self._read(path)
        p = self._ok("GET", path, status, payload, (200,))
        return GitCommit(
            sha=str(p["sha"]),
            message=str(p.get("message", "")),
            tree=str(p["tree"]["sha"]),
            parents=tuple(str(parent["sha"]) for parent in p.get("parents", [])),
        )

    def tree_sha(self, sha: str) -> str:
        """The tree of a commit (the tree check after a batch has merged, D8)."""
        return self.git_commit(sha).tree

    def pulls_for_commit(self, sha: str) -> list[int]:
        """The pull requests associated with a commit on the default branch. A squash commit and a merge
        commit both name the pull request they merged (D5)."""
        items = self._paged(f"/repos/{self.repo}/commits/{sha}/pulls")
        return [int(item["number"]) for item in items]

    def pull_files(self, number: int) -> list[str]:
        """Every path a pull request touches, including the old name of a renamed file (D3)."""
        items = self._paged(f"/repos/{self.repo}/pulls/{number}/files")
        names: list[str] = []
        for item in items:
            names.append(str(item.get("filename", "")))
            if item.get("previous_filename"):
                names.append(str(item["previous_filename"]))
        return names

    def _workflow_run(self, item: dict[str, Any]) -> WorkflowRun:
        return WorkflowRun(
            id=int(item["id"]),
            status=str(item.get("status") or ""),
            conclusion=str(item.get("conclusion") or ""),
            attempt=int(item.get("run_attempt") or 1),
            head_branch=str(item.get("head_branch") or ""),
            head_sha=str(item.get("head_sha") or ""),
            html_url=str(item.get("html_url") or ""),
        )

    def e2e_runs(self, head_sha: str) -> list[WorkflowRun]:
        """The runs of `e2e.yml` on a commit, newest first. Needs `actions: read` on the read token."""
        path = (
            f"/repos/{self.repo}/actions/workflows/{E2E_WORKFLOW}/runs?head_sha={urllib.parse.quote(head_sha, safe='')}"
        )
        items = self._paged(path, "workflow_runs")
        return sorted((self._workflow_run(item) for item in items), key=lambda r: r.id, reverse=True)

    def workflow_run(self, run_id: int) -> WorkflowRun:
        path = f"/repos/{self.repo}/actions/runs/{int(run_id)}"
        status, payload = self._read(path)
        return self._workflow_run(self._ok("GET", path, status, payload, (200,)))

    # --- writes (the basita-train App's token) ---------------------------------------------------

    def update_branch(self, number: int, expected_sha: str) -> bool:
        """Merge the base into the pull request's branch. True when GitHub accepted it (202), False on
        a merge conflict. Any other answer, including a moved head, raises."""
        path = f"/repos/{self.repo}/pulls/{number}/update-branch"
        status, payload = self._write("PUT", path, {"expected_head_sha": expected_sha})
        if status == 202:
            return True
        if status == 422 and "conflict" in self._message(payload).lower():
            return False
        raise GitHubError(f"PUT {path} -> {status} {self._message(payload)}".strip())

    def add_label(self, number: int, label: str) -> None:
        path = f"/repos/{self.repo}/issues/{number}/labels"
        status, payload = self._write("POST", path, {"labels": [label]})
        self._ok("POST", path, status, payload, (200, 201))

    def remove_label(self, number: int, label: str) -> None:
        path = f"/repos/{self.repo}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}"
        status, payload = self._write("DELETE", path)
        if status != 404:  # not on the pull request any more: nothing to remove
            self._ok("DELETE", path, status, payload, (200, 204))

    def comment(self, number: int, body: str) -> None:
        path = f"/repos/{self.repo}/issues/{number}/comments"
        status, payload = self._write("POST", path, {"body": body})
        self._ok("POST", path, status, payload, (201,))

    # --- batch-mode writes: train/run only, checked before any call (D2, D4, D7, D9) ------------------

    @staticmethod
    def _fence_ref(what: str, ref: str) -> None:
        if ref != TRAIN_REF:
            raise RefusedWrite(f"{what}: refused, the train writes {TRAIN_REF} only (asked: {ref!r})")

    def set_train_ref(self, sha: str, ref: str = TRAIN_REF) -> None:
        """Force `train/run` to `sha` (the batch base). Creates the branch the first time."""
        self._fence_ref("update-ref", ref)
        if not sha or not all(c in "0123456789abcdef" for c in sha):
            raise RefusedWrite(f"update-ref: refused, {sha!r} is not a commit SHA")
        path = f"/repos/{self.repo}/git/refs/heads/{TRAIN_REF}"
        status, payload = self._write("PATCH", path, {"sha": sha, "force": True})
        if status == 200:
            return
        # 422 "Reference does not exist" (or 404): the branch is not there yet.
        if status not in (404, 422):
            raise GitHubError(f"PATCH {path} -> {status} {self._message(payload)}".strip())
        create = f"/repos/{self.repo}/git/refs"
        status, payload = self._write("POST", create, {"ref": f"refs/heads/{TRAIN_REF}", "sha": sha})
        self._ok("POST", create, status, payload, (201,))

    def merge_into_train(self, head_sha: str, message: str, base: str = TRAIN_REF) -> MergeResult:
        """`POST /merges` of a pinned head into train/run. GitHub builds the merge server-side, so no pull
        request code runs here. 409 is a conflict; 204 means the head is already in train/run."""
        self._fence_ref("merge", base)
        path = f"/repos/{self.repo}/merges"
        status, payload = self._write("POST", path, {"base": TRAIN_REF, "head": head_sha, "commit_message": message})
        if status == 201:
            return MergeResult("merged", str(payload["sha"]))
        if status == 204:
            return MergeResult("noop")
        if status == 409:
            return MergeResult("conflict")
        raise GitHubError(f"POST {path} -> {status} {self._message(payload)}".strip())

    def dispatch_e2e(self, workflow: str = E2E_WORKFLOW, ref: str = TRAIN_REF) -> None:
        """Start `e2e.yml` on train/run, so its `E2E (Playwright)` check lands on the train tip (D4).
        Any other workflow or ref is refused before the call (Yousef, record 5d9d0b13)."""
        if workflow != E2E_WORKFLOW:
            raise RefusedWrite(f"dispatch: refused, the train dispatches {E2E_WORKFLOW} only (asked: {workflow!r})")
        self._fence_ref("dispatch", ref)
        path = f"/repos/{self.repo}/actions/workflows/{E2E_WORKFLOW}/dispatches"
        status, payload = self._write("POST", path, {"ref": TRAIN_REF})
        self._ok("POST", path, status, payload, (200, 204))

    def rerun_failed(self, run_id: int) -> None:
        """Re-run the failed jobs of one E2E run (D7). The run is read first: it must be `e2e.yml` on
        train/run, or the call is refused."""
        run = self.workflow_run(run_id)
        self._fence_ref("rerun-failed-jobs", run.head_branch)
        workflow_runs = f"/repos/{self.repo}/actions/workflows/{E2E_WORKFLOW}/runs"
        query = f"?branch={urllib.parse.quote(TRAIN_REF, safe='')}&head_sha={urllib.parse.quote(run.head_sha, safe='')}"
        listed = self._paged(workflow_runs + query, "workflow_runs")
        if int(run_id) not in {int(item["id"]) for item in listed}:
            raise RefusedWrite(f"rerun-failed-jobs: refused, run {run_id} is not a {E2E_WORKFLOW} run on {TRAIN_REF}")
        path = f"/repos/{self.repo}/actions/runs/{int(run_id)}/rerun-failed-jobs"
        status, payload = self._write("POST", path)
        self._ok("POST", path, status, payload, (201,))
