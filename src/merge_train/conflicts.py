"""Which files would conflict when the base is merged into a pull request's head.

`git merge-tree --write-tree` (git 2.38+) does the merge in memory, in a repository that has both
commits, and prints the conflicted file names. It reads objects only: it never checks out or runs
anything from the pull request.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def conflict_files(repo: Path, base_sha: str, head_sha: str) -> list[str] | None:
    """The conflicted paths, [] when the merge is clean, None when git could not tell."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, shas come from the GitHub API
            ["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", base_sha, head_sha],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return []
    if result.returncode != 1:
        return None
    # Exit 1 also means "not something we can merge" (an unknown commit), with no tree on stdout.
    # On a real conflict line 1 is the resulting tree and the conflicted file names follow it.
    lines = result.stdout.splitlines()
    if not lines or not re.fullmatch(r"[0-9a-f]{40,64}", lines[0]):
        return None
    names = lines[1:]
    return sorted({n for n in names if n and not n.startswith(" ")})
