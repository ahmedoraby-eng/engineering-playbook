"""Command line entry point: `python -m main_guard --allowed owner-login --actor x --pusher y ...`."""

from __future__ import annotations

import argparse
from pathlib import Path

from .guard import Push, check


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="main-guard", description="Flag an update to main by an unexpected actor.")
    parser.add_argument("--allowed", required=True, help="comma-separated GitHub logins allowed to update main")
    parser.add_argument(
        "--merge-only", default="", help="comma-separated logins allowed only to merge a PR into main (BASITA-162)"
    )
    parser.add_argument("--pr-merge", default="false", help="'true' when the new head is a merged PR's merge commit")
    parser.add_argument("--ref", default="refs/heads/main")
    parser.add_argument("--actor", action="append", default=[], help="an actor of the push (repeatable)")
    parser.add_argument("--forced", default="false", help="'true' for a force push")
    parser.add_argument("--before", default="")
    parser.add_argument("--after", default="")
    parser.add_argument("--summary", help="file to append the Markdown summary to ($GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)

    allowed = frozenset(a.strip().lower() for a in args.allowed.split(",") if a.strip())
    push = Push(args.ref, tuple(a.strip() for a in args.actor), args.forced.lower() == "true", args.before, args.after)
    merge_only = frozenset(a.strip().lower() for a in args.merge_only.split(",") if a.strip())
    findings = check(push, allowed, merge_only, args.pr_merge.lower() == "true")
    for f in findings:
        print(f"::{f.severity} title=Main guard::{_escape(f.message)}")
    errors = [f for f in findings if f.severity == "error"]
    verdict = "red: " + errors[0].message if errors else f"{push.ref} updated by an allowed actor"
    if args.summary:
        with Path(args.summary).open("a", encoding="utf-8") as out:
            out.write(f"### Main guard\n\n{' '.join(verdict.split())}\n")
    print(f"main-guard: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
