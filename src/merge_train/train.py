"""The merge train's decisions (ADR-020 decision 3 and Enforcement E1).

For each wave Sherif publishes the merge order (`.github/merge-train.json`). After a merge only the
*next open pull request* in that order is brought up to date, not all of them, so a wave of N pull
requests needs about N merge-ups instead of up to N(N-1)/2. This module decides and acts for that one
pull request; it **never merges**: only Ahmed merges (AGENTS.md).

One run, for the head of the train:
  1. It is not a draft and is based on the default branch (a stacked pull request waits for its base
     to merge). Otherwise: report and touch nothing.
  2. Behind the default branch: update its branch. A conflict is reported once per head and main
     commit, with the conflicting files and the label `train-conflict` (a GitHub App cannot be an
     assignee, so the label and the comment are how the owner is told). A success is followed by one
     E2E request on the new head, when the pull request carries `run-e2e`.
  3. Up to date: when `run-e2e` is on it and no E2E has run on this head, request one (once per head).
  4. Ready (the required checks green on the current head, E2E green when labelled): one comment per
     head, «جاهز للدمج».
Every comment carries a hidden marker with the head it is about, so a second run says nothing twice.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import Config
from .github import CheckRun, GitHub, GitHubError, PullRequest

E2E_LABEL = "run-e2e"
CONFLICT_LABEL = "train-conflict"
E2E_CHECK = "E2E (Playwright)"
CI_CHECK = "ci / Build and test"
OWNER_REVIEW_CHECK = "Owner review"
REVIEW_LABEL = "reviewed-by-ahmed"
DEFAULT_SETTLE_SECONDS = 120
HEAD_POLL_SECONDS = 5
HEAD_POLL_TRIES = 12

_GOOD = {"success"}
_NEUTRAL = {"skipped", "neutral"}


def marker(kind: str, key: str) -> str:
    return f"<!-- merge-train:{kind}:{key} -->"


@dataclass(frozen=True)
class Options:
    ci_check: str = CI_CHECK
    owner_review_check: str = OWNER_REVIEW_CHECK
    e2e_check: str = E2E_CHECK
    settle_seconds: int = DEFAULT_SETTLE_SECONDS
    dry_run: bool = False


@dataclass
class Report:
    """What the run saw and did; the CLI prints it and writes it to the step summary."""

    wave: str = ""
    rows: list[tuple[int, str, str]] = field(default_factory=list)  # (number, state, note)
    actions: list[str] = field(default_factory=list)
    head: int | None = None

    def row(self, number: int, state: str, note: str = "") -> None:
        self.rows.append((number, state, note))

    def did(self, text: str) -> None:
        self.actions.append(text)

    def annotate(self, number: int, note: str) -> None:
        self.rows = [(n, state, note if n == number else old) for n, state, old in self.rows]


def _latest(runs: list[CheckRun], name: str) -> CheckRun | None:
    """The newest run of a check that actually ran (a skipped job is a filter, not a result)."""
    named = [r for r in runs if r.name == name and r.conclusion not in _NEUTRAL]
    return max(named, key=lambda r: r.started_at) if named else None


def _verdict(run: CheckRun | None) -> str:
    """ok, pending, failed or missing."""
    if run is None:
        return "missing"
    if run.status != "completed":
        return "pending"
    return "ok" if run.conclusion in _GOOD else "failed"


def _clean(name: str) -> str:
    """A file name from a pull request is data: no backticks, no line breaks in our comment."""
    return re.sub(r"[`\r\n]", "", name)


def conflict_comment(pr: PullRequest, base: str, files: list[str] | None, key: str) -> str:
    if files:
        listing = "\n".join(f"- `{_clean(f)}`" for f in files[:30])
        if len(files) > 30:
            listing += f"\n- ... و{len(files) - 30} ملفا آخر"
    else:
        listing = "- (تعذّر سرد الملفات المتعارضة؛ شغّل `git merge` محليا لرؤيتها)"
    return (
        f"{marker('conflict', key)}\n"
        f"**Merge train:** #{pr.number} هو التالي في الترتيب لكن تحديثه من `{base}` يتعارض ولا يمكن تلقائيا.\n\n"
        f"الملفات المتعارضة:\n{listing}\n\n"
        f"المسؤول: Malek (الـ label `{CONFLICT_LABEL}` على الـ PR). القطار لا يدمج شيئا ولا يحلّ التعارض؛ "
        f"يحلّه المالك ثم يدفع، وبعدها يكمل القطار وحده."
    )


def ready_comment(pr: PullRequest, base: str, passed: list[str], note: str) -> str:
    ticks = "، ".join(f"{p} ✓" for p in passed)
    body = (
        f"{marker('ready', pr.head_sha)}\n"
        f"**Merge train:** جاهز للدمج: #{pr.number} ({ticks}) على آخر commit "
        f"`{pr.head_sha[:7]}`، محدَّث مع `{base}`.\n\n"
        f"القطار لا يدمج: الدمج لأحمد وحده."
    )
    if note:
        body += f"\n\n{note}"
    return body


def run(
    gh: GitHub,
    config: Config,
    *,
    options: Options | None = None,
    conflict_files: Callable[[str, str], list[str] | None] = lambda base_sha, head_sha: None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    options = options or Options()
    report = Report(wave=config.wave)
    if not config.order:
        report.did("the order is empty: nothing to do")
        return report

    head: PullRequest | None = None
    for number in config.order:
        pr = gh.pull(number)
        if pr is None:
            report.row(number, "not found", "no such pull request in this repository")
            continue
        if pr.merged:
            report.row(number, "merged")
            continue
        if pr.state != "open":
            report.row(number, "closed", "closed without merging; skipped")
            continue
        if head is None:
            head = pr
            report.head = number
            report.row(number, "head of the train")
        else:
            report.row(number, "waiting")
    if head is None:
        report.did("every pull request in the order is merged or closed: the train is empty")
        return report

    base = gh.default_branch()
    if head.draft:
        report.annotate(head.number, "draft: waits until it is ready for review")
        report.did(f"#{head.number} is a draft: not touched")
        return report
    if head.base_ref != base:
        report.did(f"#{head.number} is stacked on {head.base_ref}: waits for that base to merge, not touched")
        return report

    comments = gh.comments(head.number)

    def said(kind: str, key: str) -> bool:
        tag = marker(kind, key)
        return any(tag in body for body in comments)

    updated = False
    if gh.behind_by(base, head.head_sha) > 0:
        main_sha = gh.branch_sha(base)
        if options.dry_run:
            report.did(f"dry run: would update #{head.number} from {base}")
            return report
        if gh.update_branch(head.number, head.head_sha):
            updated = True
            report.did(f"updated #{head.number} from {base}")
            moved = _wait_for_new_head(gh, head, sleep)
            if moved is None:
                # The old head's results belong to the old code: say nothing about readiness yet.
                # The new commit's CI completing starts the train again.
                report.did(f"#{head.number}: the new head is not visible yet; the next event continues")
                return report
            head = moved
            if CONFLICT_LABEL in head.labels:
                gh.remove_label(head.number, CONFLICT_LABEL)
        else:
            key = f"{head.head_sha[:7]}-{main_sha[:7]}"
            if not said("conflict", key):
                files = conflict_files(main_sha, head.head_sha)
                gh.comment(head.number, conflict_comment(head, base, files, key))
                report.did(f"#{head.number} conflicts with {base}: commented with the files")
            else:
                report.did(f"#{head.number} still conflicts with {base} (already reported for this head)")
            if CONFLICT_LABEL not in head.labels:
                gh.add_label(head.number, CONFLICT_LABEL)
            return report
        comments = gh.comments(head.number)
    elif CONFLICT_LABEL in head.labels and not options.dry_run:
        gh.remove_label(head.number, CONFLICT_LABEL)
        report.did(f"#{head.number} is up to date again: removed {CONFLICT_LABEL}")

    # The new head may be only seconds old: give its checks time to appear before judging them absent.
    if updated and head.head_sha:
        wait = options.settle_seconds - (now() - gh.commit_time(head.head_sha))
        if wait > 0:
            sleep(wait)

    runs = gh.check_runs(head.head_sha)
    wants_e2e = E2E_LABEL in head.labels
    e2e = _verdict(_latest(runs, options.e2e_check)) if wants_e2e else "not wanted"

    if wants_e2e and e2e == "missing" and not said("e2e", head.head_sha):
        if options.dry_run:
            report.did(f"dry run: would request E2E on #{head.number}")
        else:
            # A label already on the pull request fires nothing when added again: take it off first.
            gh.remove_label(head.number, E2E_LABEL)
            gh.add_label(head.number, E2E_LABEL)
            gh.comment(
                head.number,
                f"{marker('e2e', head.head_sha)}\n"
                f"**Merge train:** E2E مطلوب على `{head.head_sha[:7]}` (الـ label `{E2E_LABEL}` أُعيدت مرة واحدة).",
            )
            report.did(f"requested E2E on #{head.number} ({head.head_sha[:7]})")
        e2e = "pending"

    ci = _verdict(_latest(runs, options.ci_check))
    owner = _verdict(_latest(runs, options.owner_review_check))
    # A pull request that changes only documentation never starts CI (path filter): absent CI is then
    # fine, provided the head has had time to settle and Owner review (never filtered) has reported.
    ci_ok = ci == "ok" or (ci == "missing" and owner != "missing")

    verdicts = {"CI": "ok" if ci_ok else ci, "E2E": e2e, "Owner review": owner}
    for name, state in verdicts.items():
        report.did(f"#{head.number} {name}: {state}")

    if ci_ok and owner == "ok" and e2e in ("ok", "not wanted"):
        if said("ready", head.head_sha):
            report.did(f"#{head.number} is ready (already announced for {head.head_sha[:7]})")
        elif options.dry_run:
            report.did(f"dry run: would announce #{head.number} as ready")
        else:
            passed = ["CI"] + (["E2E"] if wants_e2e else []) + ["Owner review"]
            if REVIEW_LABEL in head.labels:
                passed.append(REVIEW_LABEL)
            note = _not_covered_note(gh, head, runs, options)
            gh.comment(head.number, ready_comment(head, base, passed, note))
            report.did(f"announced #{head.number} as ready ({head.head_sha[:7]})")
    return report


def _not_covered_note(gh: GitHub, head: PullRequest, runs: list[CheckRun], options: Options) -> str:
    """The ready signal covers CI, E2E (when labelled) and Owner review only. Anything else that is red or
    still running, including commit statuses such as `Jira status` (a DFP issue not yet Ready for Release),
    is named, so «جاهز للدمج» is never read as «كل شيء أخضر»."""
    own = (options.ci_check, options.owner_review_check, options.e2e_check)
    red = sorted(
        r.name for r in runs if r.status == "completed" and r.conclusion not in _GOOD | _NEUTRAL and r.name not in own
    )
    running = sorted(r.name for r in runs if r.status != "completed" and r.name not in own)
    try:
        statuses = gh.commit_statuses(head.head_sha)
    except GitHubError:
        statuses = {}
        unreadable = True
    else:
        unreadable = False
    red += sorted(f"{c} ({state})" for c, state in statuses.items() if state in ("failure", "error"))
    running += sorted(c for c, state in statuses.items() if state == "pending")
    lines = ["الإعلان يشمل CI وE2E وOwner review فقط."]
    if red:
        lines.append(f"حمراء وغير مشمولة: {'، '.join(red)}.")
    if running:
        lines.append(f"لم تنتهِ بعد وغير مشمولة: {'، '.join(running)}.")
    if unreadable:
        lines.append("تعذّرت قراءة الـ commit statuses (مثل Jira status)؛ راجعها قبل الدمج.")
    return " ".join(lines)


def _wait_for_new_head(gh: GitHub, old: PullRequest, sleep: Callable[[float], None]) -> PullRequest | None:
    """update-branch answers 202 and builds the merge commit a moment later."""
    for _ in range(HEAD_POLL_TRIES):
        sleep(HEAD_POLL_SECONDS)
        fresh = gh.pull(old.number)
        if fresh is not None and fresh.head_sha != old.head_sha:
            return fresh
    return None
