"""Command line entry point: `python -m ai_guard --diff changes.diff --pr-body body.md`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checks import Finding, run_all
from .diff import parse_diff
from .policy import Policy


def _escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def format_finding(f: Finding, fmt: str) -> str:
    if fmt == "github":
        # GitHub Actions workflow command: shows as an annotation on the PR diff.
        params = []
        if f.path:
            params.append(f"file={f.path}")
            if f.line:
                params.append(f"line={f.line}")
        params.append(f"title={f.rule}")
        return f"::{f.severity} {','.join(params)}::{_escape(f.message)}"
    where = (f" {f.path}" + (f":{f.line}" if f.line else "")) if f.path else ""
    return f"{f.severity.upper():7} [{f.rule}]{where} {f.message}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ai-guard", description="Pull-request guardrails for AI-assisted engineering."
    )
    parser.add_argument("--diff", required=True, help="unified diff file ('-' for stdin)")
    parser.add_argument("--pr-body", help="file holding the pull request description")
    parser.add_argument("--labels", default="", help="comma-separated PR labels")
    parser.add_argument("--policy", default=".ai-guard.toml", help="policy file (defaults apply if missing)")
    parser.add_argument("--format", choices=["text", "github"], default="text")
    args = parser.parse_args(argv)

    diff_text = sys.stdin.read() if args.diff == "-" else Path(args.diff).read_text(encoding="utf-8")
    body = Path(args.pr_body).read_text(encoding="utf-8") if args.pr_body else ""
    labels = {label.strip() for label in args.labels.split(",") if label.strip()}

    findings = run_all(parse_diff(diff_text), body, labels, Policy.load(args.policy))
    for finding in findings:
        print(format_finding(finding, args.format))
    errors = sum(f.severity == "error" for f in findings)
    warnings = len(findings) - errors
    print(f"ai-guard: {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0
