"""The few GitHub API calls Architecture review and its T4 fire step need, through `gh api`.

GH_TOKEN comes from the job (`github.token`). Moved out of `__main__` so `fire.py` can use it too.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable


class FetchError(RuntimeError):
    """A GitHub API read failed; the check then fails closed."""


def _run_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True)


class GitHub:
    """The few GitHub API calls the check needs, through `gh api` (GH_TOKEN from the job)."""

    def __init__(self, run: Callable[[list[str]], subprocess.CompletedProcess] = _run_gh):
        self.run = run

    def get(self, path: str, raw: bool = False):
        args = ["-H", "Accept: application/vnd.github.raw+json", path] if raw else [path]
        done = self.run(args)
        if done.returncode != 0:
            raise FetchError(f"GET {path}: {(done.stderr or done.stdout).strip()[:200]}")
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            raise FetchError(f"GET {path}: not JSON ({exc})") from exc

    def items(self, path: str) -> list[dict]:
        """All items of a paginated list, one compact JSON object per line from `--jq '.[]'`."""
        done = self.run(["--paginate", "--jq", ".[] | tojson", path])
        if done.returncode != 0:
            raise FetchError(f"GET {path}: {(done.stderr or done.stdout).strip()[:200]}")
        return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]

    def post(self, path: str, fields: list[str]) -> str:
        """POST with `-f` fields. Returns "" on success, else the error text."""
        return self.send("POST", path, fields)[1]

    def send(self, method: str, path: str, fields: list[str]) -> tuple[dict | None, str]:
        """A write with `-f` fields (raw strings, so a value starting with @ is not a file).
        Returns the answer's JSON (None if it had none) and "" on success, or None and the error text."""
        done = self.run(["-X", method, path, *[arg for f in fields for arg in ("-f", f)]])
        if done.returncode != 0:
            return None, (done.stderr or done.stdout).strip()[:200] or f"gh exited {done.returncode}"
        try:
            answer = json.loads(done.stdout) if (done.stdout or "").strip() else None
        except json.JSONDecodeError:
            answer = None
        return (answer if isinstance(answer, dict) else None), ""
