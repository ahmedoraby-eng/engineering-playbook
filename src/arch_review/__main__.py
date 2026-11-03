"""Command line entry point: `python -m arch_review --repo owner/name --pr 12 --head-sha <sha> ...`.

Run by the reusable Architecture review workflow on `pull_request_target`. It reads everything
through the GitHub API with the job's token and never checks out or runs the pull request's code.
`ARCH_REVIEW_MODE` decides what the result does: `off` checks nothing, `report` is always green and
says what it would require, `on` requests the reviewer and is red until the review covers the head.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import quote

from .fire import FIRE_MODES, Fire, FireRules, decide_fire
from .github import FetchError, GitHub
from .review import Decision, Review, change_set, decide
from .rules import ChangedFile, Classification, Rules, RulesError, classify, commit_author, commit_logins

MODES = ("off", "report", "on")


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def _command(kind: str, message: str, title: str = "Architecture review") -> str:
    message = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{kind} title={title}::{message}"


def compare_files(gh: GitHub, repo: str, base: str, sha: str) -> list[dict] | None:
    try:
        data = gh.get(f"repos/{repo}/compare/{quote(base, safe='')}...{sha}")
    except FetchError:
        return None
    return data.get("files") if isinstance(data, dict) else None


def own_bots_on(rules: Rules, pr: dict, commits: list[dict]) -> list[str]:
    """The reviewer's own bots that opened the PR or are linked to one of its commits."""
    if not rules.reviewer_own_bots:
        return []
    found = {((pr.get("user") or {}).get("login") or "").lower()}
    for commit in commits:
        found |= commit_logins(commit)
    own = sorted(found & rules.reviewer_own_bots)
    # A commit the API didn't list (it stops at 250) may be one of them.
    if not own and (pr.get("commits") or 0) > len(commits):
        own = [f"commits not listed by the API (possibly {', '.join(sorted(rules.reviewer_own_bots))})"]
    return own


def evaluate(
    gh: GitHub, repo: str, pr_number: int, head_sha: str, rules: Rules
) -> tuple[dict, Classification, Decision, list[Review]]:
    pr = gh.get(f"repos/{repo}/pulls/{pr_number}")
    if (pr.get("head") or {}).get("sha") != head_sha:
        raise FetchError(f"the PR head moved to {(pr.get('head') or {}).get('sha', '?')[:7]}; the run on it decides")
    labels = {(label.get("name") or "") for label in pr.get("labels") or []}
    commits = gh.items(f"repos/{repo}/pulls/{pr_number}/commits")
    authors = [commit_author(c) for c in commits]
    # The commits API stops at 250: a commit it didn't list may be anyone's, so it counts as outside the list.
    if (pr.get("commits") or 0) > len(commits):
        authors.append(f"({pr['commits'] - len(commits)} commits not listed by the API)")
    files = [ChangedFile.from_github(f) for f in gh.items(f"repos/{repo}/pulls/{pr_number}/files")]
    waived_by = ""
    if rules.waive_label in labels:
        events = gh.items(f"repos/{repo}/issues/{pr_number}/events")
        added = [
            (e.get("actor") or {}).get("login") or ""
            for e in events
            if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == rules.waive_label
        ]
        waived_by = added[-1] if added else ""
    classification = classify(rules, authors, files, labels, waived_by, pr.get("changed_files"))
    if not classification.required:
        return pr, classification, decide(classification, rules.reviewer, head_sha, [], lambda _: False), []

    reviews = [Review.from_github(r) for r in gh.items(f"repos/{repo}/pulls/{pr_number}/reviews")]
    base = (pr.get("base") or {}).get("ref") or ""

    def same_changes(approved: str) -> bool:
        before = change_set(compare_files(gh, repo, base, approved))
        now = change_set(compare_files(gh, repo, base, head_sha))
        return before is not None and before == now

    own = own_bots_on(rules, pr, commits)
    decision = decide(classification, rules.reviewer, head_sha, reviews, same_changes, own, rules.owner)
    return pr, classification, decision, reviews


def request_reviewer(gh: GitHub, repo: str, pr: dict, rules: Rules, decision: Decision) -> str:
    """Ask the reviewer when the decision says so. Returns a note for the summary ("" if nothing)."""
    if not decision.request:
        return ""
    requested = {(u.get("login") or "").lower() for u in pr.get("requested_reviewers") or []}
    if rules.reviewer.lower() in requested:
        return f"{rules.reviewer} is already requested"
    if pr.get("draft"):
        return f"draft: {rules.reviewer} is requested when the PR is ready for review"
    if ((pr.get("user") or {}).get("login") or "").lower() == rules.reviewer.lower():
        return f"{rules.reviewer} opened the PR and cannot review it"
    error = gh.post(f"repos/{repo}/pulls/{pr['number']}/requested_reviewers", [f"reviewers[]={rules.reviewer}"])
    return f"could not request {rules.reviewer}: {error}" if error else f"requested {rules.reviewer}"


def t4_decision(
    data: dict,
    rules: Rules,
    repo: str,
    pr: dict,
    cls: Classification,
    decision: Decision,
    reviews: list[Review],
    head_sha: str,
) -> Fire:
    """T4's fire decision for this run. A bad `t4` block, or any surprise, stops T4 only, never the check."""
    try:
        return decide_fire(rules, FireRules.from_json(data), repo, pr, cls, decision, reviews, head_sha)
    except RulesError as exc:
        return Fire(f"nothing to start: {_one_line(exc)}")
    except Exception as exc:  # noqa: BLE001 - T4 must not change the check's result
        return Fire(f"nothing to start: unexpected {type(exc).__name__}")


def summary_lines(
    mode: str,
    rules: Rules | None,
    cls: Classification | None,
    decision: Decision | None,
    head_sha: str,
    note: str,
    error: str,
    fire: Fire | None = None,
) -> list[str]:
    def cell(value: str) -> str:
        return _one_line(value).replace("|", "\\|")

    rows = [("Mode", mode)]
    if cls:
        rows += [
            ("Class", f"{cls.cls}" + (f" (effort {cls.effort})" if cls.effort else "")),
            ("Why", "; ".join(cls.reasons)),
        ]
    if rules:
        rows.append(("Reviewer", rules.reviewer))
    if decision:
        rows.append(("State", decision.status))
        if decision.reviewed_sha:
            rows.append(("Last reviewed", decision.reviewed_sha[:12]))
    if error:
        rows.append(("Error", error))
    if note:
        rows.append(("Request", note))
    if fire:
        rows.append(("T4", fire.describe()))
    rows.append(("Head", head_sha[:12]))
    return ["### Architecture review", "", "| | |", "|---|---|", *[f"| {k} | {cell(v)} |" for k, v in rows], ""]


def main(argv: list[str] | None = None, gh: GitHub | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arch-review", description="Soft check: the architect's review of a PR.")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    parser.add_argument("--head-sha", required=True, help="the commit this check run belongs to")
    parser.add_argument("--rules-ref", required=True, help="base-branch commit to read the rules from")
    parser.add_argument("--rules-path", default=".github/review-rules.json")
    parser.add_argument("--mode", default="", help="off, report or on (empty means report)")
    parser.add_argument("--no-request", action="store_true", help="never request the reviewer, only wait")
    parser.add_argument("--summary", help="file to append the Markdown summary to ($GITHUB_STEP_SUMMARY)")
    parser.add_argument("--t4-mode", default="", help="T4_FIRE: off, dry-run or on (empty means off)")
    parser.add_argument("--t4-out", help="file to write T4's fire decision to, for the fire step")
    args = parser.parse_args(argv)
    gh = gh or GitHub()
    # T4 (BASITA-161): with T4_FIRE off nothing below changes; an unknown value counts as off here and
    # the fire step reports it.
    t4_mode = (args.t4_mode or "off").strip().lower()
    t4_on = t4_mode in FIRE_MODES and t4_mode != "off"
    fire = None

    mode = (args.mode or "report").strip().lower()
    rules = cls = decision = None
    note = error = ""
    if mode not in MODES:
        error = f"ARCH_REVIEW_MODE is '{_one_line(args.mode)}'; use off, report or on"
    elif mode != "off":
        try:
            ref = quote(args.rules_ref, safe="")
            data = gh.get(f"repos/{args.repo}/contents/{args.rules_path}?ref={ref}", raw=True)
            rules = Rules.from_json(data)
            pr, cls, decision, reviews = evaluate(gh, args.repo, args.pr, args.head_sha, rules)
            if t4_on:
                fire = t4_decision(data, rules, args.repo, pr, cls, decision, reviews, args.head_sha)
            if mode == "on" and not args.no_request:
                note = request_reviewer(gh, args.repo, pr, rules, decision)
            elif decision.request:
                note = "waits for a review" if args.no_request else f"would request {rules.reviewer}"
        except (FetchError, RulesError) as exc:
            error = _one_line(exc)
        except Exception as exc:  # noqa: BLE001 - an odd API answer must not turn report mode red
            error = _one_line(f"unexpected {type(exc).__name__}: {exc}")

    if args.summary:
        with Path(args.summary).open("a", encoding="utf-8") as out:
            out.write("\n".join(summary_lines(mode, rules, cls, decision, args.head_sha, note, error, fire)) + "\n")
    if fire and args.t4_out:
        Path(args.t4_out).write_text(json.dumps(fire.to_json()), encoding="utf-8")

    if mode == "off" and not error:
        print("Architecture review is off (ARCH_REVIEW_MODE=off): nothing checked.")
        return 0
    state = "error" if error else decision.state
    facts = {
        "mode": mode,
        "class": cls.cls if cls else "",
        "effort": cls.effort if cls else "",
        "state": state,
        "head": args.head_sha,
        "reviewed": decision.reviewed_sha if decision else "",
    }
    if fire:
        facts.update({"t4": fire.kind, "round": str(fire.round or "")})
    # One machine-readable line for the flow guard and pickup; they read it from the check run.
    print(_command("notice", "arch-review " + " ".join(f"{k}={v or '-'}" for k, v in facts.items())))
    message = error or decision.status
    if note:
        message += f" ({note})"
    if fire and fire.capped:
        message += f" ({fire.reason})"
    if mode == "report":
        verdict = "green" if not error and decision.green else "red"
        print(
            _command(
                "notice", f"report mode, always green; with ARCH_REVIEW_MODE=on this would be {verdict}: {message}"
            )
        )
        return 0
    if error or not decision.green:
        print(_command("error", message))
        return 1
    print(_one_line(f"Architecture review: {message}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
