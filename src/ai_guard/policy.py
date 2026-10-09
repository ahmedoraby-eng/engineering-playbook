"""Guardrail policy, loaded from a repository's `.ai-guard.toml`."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    # Files that count as production source and as tests.
    source_globs: tuple[str, ...] = ("src/*", "app/*", "apps/*", "lib/*")
    test_globs: tuple[str, ...] = ("tests/*", "test/*", "*_test.*", "*.test.*", "test_*")
    # Sensitive areas: AI-assisted changes here need an explicit human-review label.
    protected_globs: tuple[str, ...] = ("*auth*", "*migration*", "*permission*", ".github/workflows/*")
    human_review_label: str = "human-reviewed"
    # AI-assisted PRs above this size get a warning: big generated diffs are hard to review well.
    max_ai_changed_lines: int = 400
    # Additional secret regexes on top of the built-in set.
    extra_secret_patterns: tuple[str, ...] = ()
    # Paths never scanned for secrets (e.g. scanner fixtures).
    secret_scan_ignore: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, path: str | Path | None) -> Policy:
        if path is None or not Path(path).exists():
            return cls()
        data = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("policy", {})
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown policy keys: {', '.join(sorted(unknown))}")
        values = {k: tuple(v) if isinstance(v, list) else v for k, v in data.items()}
        return cls(**values)


def matches(path: str, globs: tuple[str, ...]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch(path, g) or fnmatch(name, g) for g in globs)
