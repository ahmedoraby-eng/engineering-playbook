"""The few GitHub REST calls the merge train makes, over the standard library.

Two tokens, by design (ADR-020 E1): reads use the workflow's `GITHUB_TOKEN` (read permissions), and
writes use the `basita-train` App's token, because a push or a label made with `GITHUB_TOKEN` does not
start other workflows. The App therefore needs only contents and pull-requests write.

The write methods are exactly: update a pull request's branch, add or remove a label, post a comment.
There is no merge method on purpose: only Ahmed merges (AGENTS.md); a test pins this list.
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


class GitHubError(RuntimeError):
    """A call failed. The message never carries a token."""


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
    ) -> None:
        if not repo or repo.count("/") != 1:
            raise ValueError("repo must be 'owner/name'")
        self.repo = repo
        self._read_token = read_token
        self._write_token = write_token
        self._transport = transport
        self._sleep = sleep

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
        items = self._paged(f"/repos/{self.repo}/issues/{number}/comments")
        return [str(item.get("body", "")) for item in items]

    def check_runs(self, sha: str) -> list[CheckRun]:
        items = self._paged(f"/repos/{self.repo}/commits/{sha}/check-runs", "check_runs")
        return [
            CheckRun(
                name=item["name"],
                status=item["status"],
                conclusion=item.get("conclusion") or "",
                started_at=item.get("started_at") or "",
            )
            for item in items
        ]

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
