"""Minimal unified-diff parser: which files changed and which lines were added."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class FileChange:
    path: str
    added: list[tuple[int, str]] = field(default_factory=list)
    removed: int = 0
    deleted: bool = False

    @property
    def changed_lines(self) -> int:
        return len(self.added) + self.removed


def parse_diff(text: str) -> list[FileChange]:
    """Parse `git diff` output into per-file changes with added line numbers."""
    files: list[FileChange] = []
    current: FileChange | None = None
    new_line = 0
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            # "diff --git a/x b/x": take the b/ path; refined by "+++" below.
            path = raw.split(" b/", 1)[-1]
            current = FileChange(path=path)
            files.append(current)
            continue
        if current is None:
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current.deleted = True
            elif target.startswith("b/"):
                current.path = target[2:]
            continue
        if raw.startswith("--- "):
            continue
        match = _HUNK.match(raw)
        if match:
            new_line = int(match.group(1))
            continue
        if raw.startswith("+"):
            current.added.append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            current.removed += 1
        elif raw.startswith(" "):
            new_line += 1
    return files
