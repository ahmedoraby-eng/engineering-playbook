"""Command line entry point, run by the reusable workflow:

    GH_READ_TOKEN=... GH_WRITE_TOKEN=... python -m merge_train --repo owner/name [--repo-dir DIR]

`--dry-run` makes the decisions and writes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import ConfigError, parse
from .conflicts import conflict_files
from .github import GitHub, GitHubError
from .train import Options, Report, run

CONFIG_PATH = ".github/merge-train.json"


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def summary(report: Report) -> str:
    lines = [f"### Merge train {report.wave}".rstrip(), ""]
    if report.rows:
        lines += ["| PR | State | Note |", "|---|---|---|"]
        lines += [f"| #{n} | {state} | {note} |" for n, state, note in report.rows]
        lines.append("")
    lines += [f"- {a}" for a in report.actions]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="merge-train", description="Bring the next PR in the wave's order up to date."
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--config-path", default=CONFIG_PATH)
    parser.add_argument("--repo-dir", default=".", help="a clone with the history (to list conflicting files)")
    parser.add_argument("--settle-seconds", type=int, default=Options.settle_seconds)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary", help="file to append the Markdown summary to ($GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)

    read_token = os.environ.get("GH_READ_TOKEN", "")
    write_token = os.environ.get("GH_WRITE_TOKEN", "")
    if not read_token or not (write_token or args.dry_run):
        print("::error title=Merge train::GH_READ_TOKEN and GH_WRITE_TOKEN must be set")
        return 2
    gh = GitHub(args.repo, read_token, write_token or read_token)
    try:
        base = gh.default_branch()
        text = gh.read_file(args.config_path, base)
        if text is None:
            print(f"merge-train: no {args.config_path} on {base}: nothing to do")
            return 0
        config = parse(text)
        repo_dir = Path(args.repo_dir)
        report = run(
            gh,
            config,
            options=Options(settle_seconds=args.settle_seconds, dry_run=args.dry_run),
            conflict_files=lambda base_sha, head_sha: conflict_files(repo_dir, base_sha, head_sha),
        )
    except ConfigError as exc:
        print(f"::error title=Merge train::{_escape(str(exc))}")
        return 1
    except GitHubError as exc:
        print(f"::error title=Merge train::{_escape(str(exc))}")
        return 1
    print(summary(report))
    if args.summary:
        with Path(args.summary).open("a", encoding="utf-8") as out:
            out.write(summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
