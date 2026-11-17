"""Batch mode of the merge train (Yousef's amendment D1-D9, 17/11/2026; standards/merge-train.md).

One full E2E covers a batch of k pull requests (default 3, max 4), and no pull-request branch is ever
updated. The train still never merges: it says who is ready, in order.

State. The only state is the branch `train/run` (TRAIN_REF, a constant). Each batch force-points it at
the default branch's commit (the *base*) and merges each member's pinned head into it through the API,
one merge commit per member, with the subject

    train <wave>#<batch>: #<N>@<head sha>

so the tip's first-parent chain *is* the manifest: base, then the members in order. Every run reads it
back (`parse_manifest`) and decides from it, the pull requests, the checks and `main`, so a run can be
repeated or interrupted at any point.

Bisect state (D7) lives in the same chain. When a batch is red twice, the train rebuilds `train/run` with
only a prefix of its members ([1], then [1,2], ...), from the same base, and each merge commit of such a
prefix build carries one more line in its body:

    Train-Bisect: <tip sha of the full batch that was red twice> #<N>@<sha> #<N>@<sha> ...

listing the full batch in order. A tip with that line is prefix j of that full batch, j = its member
count; a tip without it is a full batch. With the E2E result on the tip, `next_step` gives the next move
(wait, dispatch, re-run, the next prefix, or the verdict: the green prefix and the culprit). The batch
number stays the same through the bisect; a new batch takes the next number of the wave.

Comment markers, as in the per-PR mode, are hidden `<!-- merge-train:<kind>:<key> -->` lines and count only
when the train's own App wrote them.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .github import TRAIN_REF, CheckRun, GitCommit, GitHub, GitHubError, MergeResult, PullRequest, WorkflowRun
from .train import (
    _GOOD,
    _NEUTRAL,
    CONFLICT_LABEL,
    E2E_LABEL,
    REVIEW_LABEL,
    Options,
    Report,
    _clean,
    _latest,
    _not_covered_note,
    _verdict,
    marker,
    ready_comment,
)

RED_LABEL = "train-red"
MAX_BATCH = 4
WORKFLOWS_DIR = ".github/workflows/"
GATE_STALE_SECONDS = 2 * 3600  # a gate pending longer than this gets an ATTENTION line (Yousef, note 1)
MAIN_WALK_LIMIT = 30  # main commits read back to the batch base; more than that is "main moved by others"
PLAIN = "no run-e2e: plain ready check"

_SUBJECT = re.compile(r"^train (?P<wave>[A-Za-z0-9._-]*)#(?P<batch>\d+): #(?P<number>\d+)@(?P<sha>[0-9a-f]{40,64})$")
_BISECT = re.compile(r"^Train-Bisect: (?P<tip>[0-9a-f]{40,64})(?P<members>(?: #\d+@[0-9a-f]{40,64})+)\s*$", re.M)
_MEMBER = re.compile(r"#(\d+)@([0-9a-f]{40,64})")


def wave_token(wave: str) -> str:
    """The wave as it appears in commit messages and markers: no spaces, no '#', nothing that ends a marker."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", wave)


# --- the manifest -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    number: int
    sha: str

    def __str__(self) -> str:
        return f"#{self.number}@{self.sha}"


@dataclass(frozen=True)
class Manifest:
    """What train/run holds: the base, then one merge commit per member, in order."""

    wave: str
    batch: int
    base: str
    members: tuple[Member, ...]
    commits: tuple[str, ...]  # the merge commit of each member (same index)
    trees: tuple[str, ...]  # the tree of each of those commits
    bisect_of: tuple[Member, ...] = ()  # the full batch, when this is a prefix build
    full_tip: str = ""  # the tip of that full batch (its red runs stay readable by SHA)
    distrust: str = ""  # why a commit of this manifest is not the train's own (M1); "" when trusted

    @property
    def tip(self) -> str:
        return self.commits[-1]

    @property
    def label(self) -> str:
        return f"{self.wave}#{self.batch}"

    @property
    def full(self) -> tuple[Member, ...]:
        return self.bisect_of or self.members


def manifest_message(wave: str, batch: int, member: Member, bisect: tuple[str, Sequence[Member]] | None = None) -> str:
    subject = f"train {wave}#{batch}: {member}"
    if bisect is None:
        return subject
    full_tip, full = bisect
    return f"{subject}\n\nTrain-Bisect: {full_tip} " + " ".join(str(m) for m in full)


def _bisect_line(body: str) -> tuple[str, tuple[Member, ...]] | None:
    found = _BISECT.search(body)
    if not found:
        return None
    members = tuple(Member(int(n), sha) for n, sha in _MEMBER.findall(found["members"]))
    return found["tip"], members


def manifest_distrust(base: str, commits: Sequence[GitCommit], members: Sequence[Member]) -> str:
    """Yousef's M1. With no branch protection, anyone with write access could push a commit whose message
    looks like a manifest line. A manifest commit counts only when (a) its parents are exactly [the
    previous manifest commit, or the base for the first one; the pinned head its message names], and
    (b) GitHub verified its signature (`POST /merges` commits are signed by web-flow). Returns why not,
    or "" when every commit passes. `commits` and `members` are oldest first."""
    previous = base
    for commit, member in zip(commits, members, strict=True):
        if commit.parents != (previous, member.sha):
            return f"commit `{commit.sha[:7]}` على {TRAIN_REF} مش merge لـ #{member.number} فوق اللي قبله"
        if not commit.verified:
            return f"commit `{commit.sha[:7]}` على {TRAIN_REF} مش موقّع من GitHub"
        previous = commit.sha
    return ""


def parse_manifest(chain: Sequence[GitCommit]) -> Manifest | None:
    """`chain` is train/run's tip and its first parents, newest first. None when the tip is not a train
    merge (no live batch: never built, or every member of the last build conflicted). A manifest whose
    commits fail `manifest_distrust` comes back with `distrust` set: the run drops it and rebuilds."""
    matched: list[tuple[GitCommit, Member]] = []
    key = None
    for commit in chain:
        subject, _, body = commit.message.partition("\n")
        found = _SUBJECT.match(subject.strip())
        if not found:
            break
        this = (found["wave"], int(found["batch"]), _bisect_line(body))
        if key is None:
            key = this
        elif this != key:
            break
        matched.append((commit, Member(int(found["number"]), found["sha"])))
    if key is None or len(matched) > MAX_BATCH:
        return None
    matched.reverse()
    earliest = matched[0][0]
    if not earliest.parents:
        return None
    wave, batch, bisect = key
    members = tuple(m for _, m in matched)
    base = earliest.parents[0]
    return Manifest(
        wave=wave,
        batch=batch,
        base=base,
        members=members,
        commits=tuple(c.sha for c, _ in matched),
        trees=tuple(c.tree for c, _ in matched),
        bisect_of=bisect[1] if bisect else (),
        full_tip=bisect[0] if bisect else "",
        distrust=manifest_distrust(base, [c for c, _ in matched], members),
    )


# --- D7: what to do with the E2E result -----------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """kind: wait, dispatch, rerun, bisect (build `prefix` members), settled (`green` members are green;
    `culprit` is the 0-based index of the red member, or None), stuck (E2E finished with no result)."""

    kind: str
    prefix: int = 0
    green: int = 0
    culprit: int | None = None
    flaky: bool = False


def next_step(k: int, prefix: int | None, verdict: str, attempt: int) -> Step:
    """`k` members in the full batch; `prefix` is None for the full batch, else the prefix length j
    (1 <= j <= k-1). `verdict` is the E2E result on the tip (ok, failed, pending, missing, stuck) and
    `attempt` the run's attempt number.

    Full batch: green -> all green (flaky when green only on the re-run); red on the first attempt -> one
    re-run; red again -> prefix [1] (or, for k = 1, the only member is the culprit). Prefix j: green -> the
    next prefix, or at j = k-1 the last member is the culprit; red -> member j is the culprit. So at most
    k-1 prefix runs, and prefixes are not re-run."""
    if verdict in ("pending", "missing", "stuck"):
        return Step({"pending": "wait", "missing": "dispatch", "stuck": "stuck"}[verdict])
    green = verdict == "ok"
    if prefix is None:
        if green:
            return Step("settled", green=k, flaky=attempt >= 2)
        if attempt <= 1:
            return Step("rerun")
        if k == 1:
            return Step("settled", green=0, culprit=0)
        return Step("bisect", prefix=1)
    if green:
        if prefix >= k - 1:
            return Step("settled", green=prefix, culprit=k - 1)
        return Step("bisect", prefix=prefix + 1)
    return Step("settled", green=prefix - 1, culprit=prefix - 1)


# --- D1 / D3: who joins the next batch -------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """What a run knows about one pull request of the order."""

    number: int
    pr: PullRequest | None
    ci: str = "missing"
    owner: str = "missing"
    red_here: bool = False  # carries train-red, and the train named this very head as a culprit
    conflict_here: bool = False  # the train reported a conflict for this head on this main


def candidacy(f: Facts, base_branch: str) -> str | None:
    """None when the pull request is a candidate (D1); otherwise why not."""
    pr = f.pr
    if pr is None:
        return "not found"
    if pr.merged:
        return "merged"
    if pr.state != "open":
        return "closed"
    if pr.draft:
        return "draft"
    if pr.base_ref != base_branch:
        return f"stacked on {pr.base_ref}"
    if E2E_LABEL not in pr.labels:
        return PLAIN
    if f.ci != "ok":
        return f"CI {f.ci}"
    if f.owner != "ok":
        return f"Owner review {f.owner}"
    if f.red_here:
        return f"{RED_LABEL} on this head"
    if f.conflict_here:
        return "conflicts in the train (this head on this main)"
    return None


@dataclass(frozen=True)
class Selection:
    batch: tuple[PullRequest, ...]
    skipped: tuple[tuple[int, str], ...]  # not candidates, with the reason
    waiting: tuple[tuple[int, str], ...]  # candidates for a later batch


def select_batch(
    order: Sequence[int],
    facts: Mapping[int, Facts],
    base_branch: str,
    size: int,
    changes_workflows: Callable[[int], bool],
) -> Selection:
    """The first `size` candidates in the published order (D1). A candidate that changes
    `.github/workflows/**` goes alone (D3): first in line, it is a batch of one; later, it ends the batch
    before it and goes alone next time. `changes_workflows` is asked only about candidates up to there."""
    skipped: list[tuple[int, str]] = []
    candidates: list[PullRequest] = []
    for number in order:
        f = facts[number]
        reason = candidacy(f, base_branch)
        if reason is None and f.pr is not None:
            candidates.append(f.pr)
        else:
            skipped.append((number, reason or "not found"))
    batch: list[PullRequest] = []
    waiting: list[tuple[int, str]] = []
    rest = list(candidates)
    while rest and len(batch) < size:
        pr = rest.pop(0)
        if changes_workflows(pr.number):
            if batch:
                waiting.append((pr.number, "changes .github/workflows: goes alone in its own batch"))
            else:
                batch.append(pr)  # alone: a batch of one
            break
        batch.append(pr)
    waiting += [(pr.number, "candidate: waits for the next batch") for pr in rest]
    return Selection(tuple(batch), tuple(skipped), tuple(waiting))


# --- D5 / D6: is the live batch still valid ------------------------------------------------------------


@dataclass(frozen=True)
class MainStep:
    sha: str
    pulls: frozenset[int]


def main_moved_by(path: Sequence[MainStep], members: Sequence[Member]) -> tuple[int, int, bool]:
    """How `main` moved since the base. `path` is main's first-parent commits after the base, oldest
    first. Returns (how many members, in order, the path starts with; the index of the last commit of the
    last of them, or -1; whether the whole path is those members). Squash and merge commits are one commit
    per member; a rebase merge is several in a row for the same member."""
    count, end = 0, -1
    for index, step in enumerate(path):
        if count < len(members) and members[count].number in step.pulls:
            count, end = count + 1, index
        elif count and members[count - 1].number in step.pulls and end == index - 1:
            end = index
        else:
            return count, end, False
    return count, end, True


@dataclass(frozen=True)
class Assessment:
    reason: str | None  # why the batch is dropped (D6), None when it stands
    merged: int = 0  # members already on main, in order
    end: int = -1  # index in the main path of the last of them
    pending: tuple[int, ...] = ()  # members whose CI or Owner review is running again (hold, don't drop)


def assess(
    manifest: Manifest,
    members: Sequence[Member],
    pulls: Mapping[int, PullRequest | None],
    gates: Mapping[int, tuple[str, str]],
    order: Sequence[int],
    wave: str,
    base_branch: str,
    path: Sequence[MainStep] | None,
) -> Assessment:
    """D6: the batch is dropped when a member's head moved; a member is closed, draft, or lost CI or Owner
    review green; main moved by a commit that is not the next member; or the published order changed
    (another wave, a member left the order, or the members' order changed). A check that is running again
    is not a loss: it holds the ready signal until it is green (an Owner review re-run on a label event
    would otherwise drop every batch). `gates` maps a member to its (CI, Owner review) verdicts."""
    if manifest.wave != wave:
        return Assessment("الـ wave اتغيّرت: الترتيب المنشور اتغيّر")
    positions = []
    for m in members:
        pr = pulls.get(m.number)
        if pr is not None and pr.merged:
            continue
        if m.number not in order:
            return Assessment(f"#{m.number} اتشال من الترتيب المنشور")
        positions.append(order.index(m.number))
    if positions != sorted(positions):
        return Assessment("ترتيب الأعضاء في الترتيب المنشور اتغيّر")
    if path is None:
        return Assessment("main اتحرك بـ commits مش من الـ batch")
    count, end, clean = main_moved_by(path, members)
    pending: list[int] = []
    for index, m in enumerate(members):
        pr = pulls.get(m.number)
        if pr is None:
            return Assessment(f"#{m.number} مش موجود")
        if index < count:
            if not pr.merged:
                return Assessment(f"main اتحرك بـ commit مرتبط بـ #{m.number} من غير ما يتدمج")
            continue
        if pr.merged:
            return Assessment(f"#{m.number} اتدمج قبل دوره")
        if pr.state != "open":
            return Assessment(f"#{m.number} اتقفل")
        if pr.draft:
            return Assessment(f"#{m.number} بقى draft")
        if pr.head_sha != m.sha:
            return Assessment(f"اتعمل push على #{m.number}")
        if pr.base_ref != base_branch:
            return Assessment(f"base بتاع #{m.number} اتغيّر")
        ci, owner = gates.get(m.number, ("missing", "missing"))
        for name, state in (("CI", ci), ("Owner review", owner)):
            if state in ("failed", "missing"):
                return Assessment(f"{name} على #{m.number} مبقاش أخضر")
        if "pending" in (ci, owner):
            pending.append(m.number)
    if not clean and count < len(members):
        return Assessment("main اتحرك بـ commit مش العضو التالي في الـ batch")
    return Assessment(None, count, end, tuple(pending))


# --- comments ------------------------------------------------------------------------------------------


def ready_key(member: Member, manifest: Manifest) -> str:
    """Keyed on head + batch (the batch's label and its tip, which no other build shares)."""
    return f"{member.sha}@{manifest.label}:{manifest.tip[:12]}"


def batch_ready_comment(
    pr: PullRequest,
    manifest: Manifest,
    green: Sequence[Member],
    index: int,
    passed: list[str],
    note: str,
    *,
    flaky: bool,
    run_url: str,
) -> str:
    member = green[index]
    k = len(green)
    after = f"بعد #{green[index - 1].number}" if index else "الأول في الـ batch"
    sequence = " ثم ".join(f"#{m.number}" for m in green)
    ticks = "، ".join(f"{p} ✓" for p in passed)
    evidence = f"E2E الكامل أخضر على الـ batch (`{manifest.tip[:7]}`"
    evidence += f"، {run_url})" if run_url else ")"
    if manifest.bisect_of:
        evidence = f"E2E أخضر على أول {k} من الـ batch (bisect، من نفس الـ base وبنفس الترتيب)"
    body = (
        f"{marker('ready', ready_key(member, manifest))}\n"
        f"**Merge train:** جاهز للدمج (batch {manifest.label}, {index + 1}/{k}): {after}\n\n"
        f"#{pr.number} على `{member.sha[:7]}` ({ticks}). {evidence}.\n"
        f"الدمج بالترتيب بالظبط: {sequence}. القطار لا يدمج."
    )
    if flaky:
        body += "\n\nتنبيه: E2E فشل مرة ونجح في الإعادة (flaky)؛ التفاصيل في تعليق الـ flaky."
    if note:
        body += f"\n\n{note}"
    return body


def withdrawn_comment(member: Member, manifest: Manifest, reason: str) -> str:
    return (
        f"{marker('ready', ready_key(member, manifest) + ':withdrawn')}\n"
        f"**Merge train:** سُحب: batch اتلغى ({reason}).\n\n"
        f"متدمجش #{member.number} على إعلان «جاهز للدمج» بتاع batch {manifest.label}؛ القطار هيبني batch جديد."
    )


def _listing(files: Sequence[str]) -> str:
    listing = "\n".join(f"- `{_clean(f)}`" for f in files[:30])
    if len(files) > 30:
        listing += f"\n- ... و{len(files) - 30} ملفا آخر"
    return listing


def batch_conflict_comment(
    member: Member,
    base_branch: str,
    base: str,
    label: str,
    earlier: Sequence[Member],
    files: list[str] | None,
    key: str,
) -> str:
    before = f" بعد {'، '.join(f'#{m.number}' for m in earlier)}" if earlier else ""
    if files:
        detail = f"الملفات المتعارضة:\n{_listing(files)}"
    elif files == [] and earlier:
        detail = "مفيش تعارض مع `" + base_branch + "` نفسه: التعارض مع الأعضاء اللي قبله في الـ batch."
    else:
        detail = "تعذّر سرد الملفات المتعارضة؛ شغّل `git merge` محليا لرؤيتها."
    return (
        f"{marker('conflict', key)}\n"
        f"**Merge train:** #{member.number} (`{member.sha[:7]}`) لا يندمج نظيفا في batch {label} "
        f"على `{base_branch}` (`{base[:7]}`){before}، فخرج من الـ batch.\n\n{detail}\n\n"
        f"المسؤول: Malek (الـ label `{CONFLICT_LABEL}`). القطار لا يدمج ولا يحلّ التعارض؛ بعد push جديد "
        f"أو تحرّك `{base_branch}` يجرّبه تاني في batch جاي."
    )


# --- the run -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class E2EState:
    verdict: str
    attempt: int
    run: WorkflowRun | None


class _BatchRun:
    def __init__(
        self,
        gh: GitHub,
        config: Config,
        options: Options,
        conflict_files: Callable[[str, str], list[str] | None],
        now: Callable[[], float],
    ) -> None:
        self.gh = gh
        self.config = config
        self.options = options
        self.conflict_files = conflict_files
        self.now = now
        self.wave = wave_token(config.wave)
        self.report = Report(wave=config.wave)
        self._pulls: dict[int, PullRequest | None] = {}
        self._checks: dict[str, list[CheckRun]] = {}
        self._comments: dict[int, list[str]] = {}
        self.notes: dict[int, tuple[str, str]] = {}
        self.live_members: tuple[Member, ...] = ()
        self.live_label = ""

    # reads, cached for one run
    def pull(self, number: int) -> PullRequest | None:
        if number not in self._pulls:
            self._pulls[number] = self.gh.pull(number)
        return self._pulls[number]

    def checks(self, sha: str) -> list[CheckRun]:
        if sha not in self._checks:
            self._checks[sha] = self.gh.check_runs(sha)
        return self._checks[sha]

    def gates(self, sha: str) -> tuple[str, str]:
        runs = self.checks(sha)
        o = self.options
        return _verdict(_latest(runs, o.ci_check)), _verdict(_latest(runs, o.owner_review_check))

    def said(self, number: int, kind: str, key: str) -> bool:
        if number not in self._comments:
            self._comments[number] = self.gh.comments(number)
        tag = marker(kind, key)
        return any(tag in body for body in self._comments[number])

    # writes: each one is reported; a dry run only says what it would do
    def comment(self, number: int, body: str, what: str) -> None:
        if self.options.dry_run:
            self.report.did(f"dry run: would comment on #{number}: {what}")
        else:
            self.gh.comment(number, body)
            self.report.did(f"commented on #{number}: {what}")
        self._comments.setdefault(number, []).append(body)

    def label(self, number: int, label: str, *, add: bool) -> None:
        pr = self.pull(number)
        if pr is None or (label in pr.labels) == add:
            return
        verb = "add" if add else "remove"
        if self.options.dry_run:
            self.report.did(f"dry run: would {verb} {label} on #{number}")
        else:
            if add:
                self.gh.add_label(number, label)
            else:
                self.gh.remove_label(number, label)
            self.report.did(f"{verb} {label} on #{number}")
        labels = pr.labels | {label} if add else pr.labels - {label}
        self._pulls[number] = PullRequest(**{**pr.__dict__, "labels": labels})

    # -- the run --------------------------------------------------------------------------------------

    def go(self) -> Report:
        self.base_branch = self.gh.default_branch()
        self.main_sha = self.gh.branch_sha(self.base_branch)
        manifest = self.read_manifest()
        batch_no = 1
        free = True
        if manifest is not None:
            if manifest.wave == self.wave:
                batch_no = manifest.batch + 1
            free = self.live(manifest)
        elif not self.config.order:
            self.report.did("the order is empty: nothing to do")
            return self.report
        facts = {n: self.facts(n) for n in self.config.order}
        if free:
            self.build_next(facts, batch_no)
        self.plain_ready(facts)
        for n in self.config.order:
            state, note = self.notes.get(n, ("", ""))
            if not state:
                f = facts[n]
                state = candidacy(f, self.base_branch) or "candidate"
            self.report.row(n, state, note)
        return self.report

    def read_manifest(self) -> Manifest | None:
        tip = self.gh.ref_sha(TRAIN_REF)
        if tip is None:
            return None
        chain: list[GitCommit] = []
        sha = tip
        for _ in range(MAX_BATCH + 1):
            commit = self.gh.git_commit(sha)
            chain.append(commit)
            if not _SUBJECT.match(commit.message.partition("\n")[0].strip()) or not commit.parents:
                break
            sha = commit.parents[0]
        return parse_manifest(chain)

    def facts(self, number: int) -> Facts:
        pr = self.pull(number)
        if pr is None or pr.state != "open":
            return Facts(number, pr)
        ci, owner = self.gates(pr.head_sha)
        red = RED_LABEL in pr.labels and self.said(number, "red", pr.head_sha)
        conflict = self.said(number, "conflict", f"{pr.head_sha[:7]}-{self.main_sha[:7]}")
        return Facts(number, pr, ci, owner, red, conflict)

    def e2e(self, tip: str) -> E2EState:
        runs = self.gh.e2e_runs(tip)
        run = runs[0] if runs else None
        check = _verdict(_latest(self.gh.check_runs(tip), self.options.e2e_check))
        if run is None:
            verdict = check
        elif run.status != "completed":
            verdict = "pending"
        elif check == "missing":
            verdict = "stuck"
        else:
            verdict = check
        return E2EState(verdict, run.attempt if run else 1, run)

    def main_path(self, base: str) -> tuple[MainStep, ...] | None:
        """main's first-parent commits after `base`, oldest first; None when base is not within reach."""
        steps: list[MainStep] = []
        sha = self.main_sha
        for _ in range(MAIN_WALK_LIMIT):
            if sha == base:
                return tuple(reversed(steps))
            steps.append(MainStep(sha, frozenset(self.gh.pulls_for_commit(sha))))
            parents = self.gh.git_commit(sha).parents
            if not parents:
                return None
            sha = parents[0]
        return None

    def live(self, m: Manifest) -> bool:
        """Carry the batch on train/run one step. True when train/run is free for the next batch (the
        batch is finished or dropped), False while it is still live."""
        full = m.full
        if m.distrust:
            self.report.did(f"ATTENTION: the manifest on {TRAIN_REF} is not trusted ({m.distrust}); rebuilding")
            self.drop(m, full, f"الـ manifest على {TRAIN_REF} مش موثوق: {m.distrust}")
            return True
        if m.bisect_of and not (0 < len(m.members) < len(full) and m.members == full[: len(m.members)]):
            self.drop(m, full, "بناء الـ bisect مش مطابق لأعضاء الـ batch")
            return True
        e2e = self.e2e(m.tip) if m.wave == self.wave else E2EState("pending", 1, None)
        step = next_step(len(full), len(m.members) if m.bisect_of else None, e2e.verdict, e2e.attempt)
        check_set = full[: step.green] if step.kind == "settled" else full
        pulls = {mem.number: self.pull(mem.number) for mem in full}
        gates = {}
        for mem in check_set:
            pr = pulls[mem.number]
            if pr is not None and pr.state == "open" and pr.head_sha == mem.sha:
                gates[mem.number] = self.gates(mem.sha)
        path = self.main_path(m.base) if m.wave == self.wave else None
        a = assess(m, check_set, pulls, gates, self.config.order, self.wave, self.base_branch, path)
        for i, mem in enumerate(full):
            self.notes[mem.number] = (f"batch {m.label} {i + 1}/{len(full)}", "")
        if a.reason:
            self.drop(m, check_set, a.reason)
            return True
        self.report.head = full[0].number
        what = f"batch {m.label}" + (f" (bisect prefix {len(m.members)}/{len(full)})" if m.bisect_of else "")
        self.report.did(f"{what}: E2E {e2e.verdict} (attempt {e2e.attempt}) on {m.tip[:7]}")
        if step.kind == "wait":
            check = _latest(self.gh.check_runs(m.tip), self.options.e2e_check)
            since = check.started_at if check is not None else (e2e.run.created_at if e2e.run else "")
            self.stale(f"E2E on batch {m.label} ({m.tip[:7]})", since)
            self.keep_live(m, full)
            return False
        if step.kind == "stuck":
            self.report.did(
                f"ATTENTION: e2e.yml finished on {m.tip[:7]} without an `{self.options.e2e_check}` result; "
                f"check that e2e.yml runs its E2E job on a workflow_dispatch of {TRAIN_REF}"
            )
            self.keep_live(m, full)
            return False
        if step.kind == "dispatch":
            age = self.now() - self.gh.commit_time(m.tip)
            if age < self.options.settle_seconds:
                self.report.did(f"batch {m.label}: E2E not visible yet on {m.tip[:7]}; waiting")
            elif self.options.dry_run:
                self.report.did(f"dry run: would dispatch e2e.yml on {TRAIN_REF}")
            else:
                self.gh.dispatch_e2e()
                self.report.did(f"dispatched e2e.yml on {TRAIN_REF} ({m.tip[:7]}): no run was found for it")
            self.keep_live(m, full)
            return False
        if step.kind == "rerun":
            if e2e.run is None:
                self.report.did(f"ATTENTION: batch {m.label} is red but its E2E run is not found to re-run")
            elif self.options.dry_run:
                self.report.did(f"dry run: would re-run the failed jobs of run {e2e.run.id}")
            else:
                self.gh.rerun_failed(e2e.run.id)
                self.report.did(f"batch {m.label} red on the first attempt: re-ran the failed jobs ({e2e.run.id})")
            self.keep_live(m, full)
            return False
        if step.kind == "bisect":
            prefix = full[: step.prefix]
            self.report.did(f"batch {m.label} red: testing the prefix {', '.join(f'#{x.number}' for x in prefix)}")
            self.build(prefix, m.batch, m.base, bisect=(m.full_tip or m.tip, full))
            self.keep_live(m, full)
            return False
        # settled
        green = full[: step.green]
        if step.culprit is not None:
            self.blame(m, full, step.culprit, e2e)
            for later in full[step.culprit + 1 :]:
                self.notes[later.number] = ("back to the queue", f"after the culprit #{full[step.culprit].number}")
        if step.flaky:
            self.flaky(m, e2e)
        if a.merged == len(green):
            if green:
                self.tree_check(m, green, a.end, path or ())
            self.report.did(f"batch {m.label} is finished: train/run is free for the next batch")
            return True
        self.keep_live(m, green)
        if a.pending:
            held = ", ".join(f"#{n}" for n in a.pending)
            self.report.did(f"batch {m.label} green; ready held while CI or Owner review runs again on {held}")
            for mem in green:
                if mem.number not in a.pending:
                    continue
                for name in (self.options.ci_check, self.options.owner_review_check):
                    run_ = _latest(self.checks(mem.sha), name)
                    if run_ is not None and run_.status != "completed":
                        self.stale(f"{name} on #{mem.number}", run_.started_at)
            return False
        self.announce(m, green, a.merged, e2e, step.flaky)
        return False

    def stale(self, what: str, since: str) -> None:
        """Note 1: the batch is held as before; a gate pending for more than 2 h is only reported."""
        try:
            started = datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return
        hours = (self.now() - started) / 3600
        if self.now() - started > GATE_STALE_SECONDS:
            self.report.did(f"ATTENTION: {what} pending for {hours:.1f} h (since {since}); the batch is held")

    def keep_live(self, m: Manifest, members: Sequence[Member]) -> None:
        self.live_members = tuple(members)
        self.live_label = m.label

    def drop(self, m: Manifest, members: Sequence[Member], reason: str) -> None:
        """D6: the batch is invalid. Its ready comments are withdrawn; the caller builds the next one."""
        self.report.did(f"batch {m.label} dropped: {reason}")
        for mem in members:
            pr = self.pull(mem.number)
            if pr is not None and pr.merged:
                continue
            key = ready_key(mem, m)
            if self.said(mem.number, "ready", key) and not self.said(mem.number, "ready", key + ":withdrawn"):
                self.comment(mem.number, withdrawn_comment(mem, m, reason), f"withdrew ready of batch {m.label}")

    def failing_tests(self, sha: str) -> list[str]:
        failed = [
            c
            for c in self.gh.check_runs(sha, all_runs=True)
            if c.name == self.options.e2e_check and c.status == "completed" and c.conclusion not in _GOOD | _NEUTRAL
        ]
        names: list[str] = []
        for c in failed[:2]:
            try:
                lines = self.gh.annotations(c.id) if c.id else []
            except GitHubError:
                lines = []
            for line in lines:
                clean = _clean(line)
                if clean and clean not in names:
                    names.append(clean)
        return names[:20]

    @staticmethod
    def _tests_block(names: list[str]) -> str:
        if not names:
            return "(تعذّرت قراءة أسماء الاختبارات من الـ annotations؛ راجع الـ run)"
        return "\n".join(f"- `{n}`" for n in names)

    def blame(self, m: Manifest, full: Sequence[Member], culprit: int, e2e: E2EState) -> None:
        mem = full[culprit]
        if culprit < len(m.members):
            sha, run = m.tip, e2e.run
            if len(full) == 1:
                how = "الـ batch من عضو واحد فشل مرتين"
            elif culprit == 0:
                how = "bisect: أول عضو لوحده أحمر"
            else:
                how = f"bisect: أول {culprit + 1} من الـ batch أحمر، وأول {culprit} أخضر"
        else:
            sha = m.full_tip
            runs = self.gh.e2e_runs(sha) if sha else []
            run = runs[0] if runs else None
            how = f"bisect: أول {culprit} من الـ batch أخضر، والـ batch كله أحمر مرتين"
        self.notes[mem.number] = ("culprit", f"{RED_LABEL}: batch {m.label}")
        if not self.said(mem.number, "red", mem.sha):
            link = run.html_url if run and run.html_url else "(الـ run مش متاح)"
            body = (
                f"{marker('red', mem.sha)}\n"
                f"**Merge train:** #{mem.number} (`{mem.sha[:7]}`) هو سبب فشل E2E في batch {m.label} ({how}).\n\n"
                f"الاختبارات الفاشلة:\n{self._tests_block(self.failing_tests(sha) if sha else [])}\n\n"
                f"الـ run: {link}\n\n"
                f"المسؤول: Malek (الـ label `{RED_LABEL}`). الـ PRs اللي بعده رجعت للطابور؛ بعد push جديد يدخل "
                f"batch جاي."
            )
            self.comment(mem.number, body, f"culprit of batch {m.label}")
        self.label(mem.number, RED_LABEL, add=True)

    def flaky(self, m: Manifest, e2e: E2EState) -> None:
        first = m.full[0].number
        self.report.did(f"ATTENTION: batch {m.label} is flaky (red, then green on the re-run): a Salma flake ticket")
        if self.said(first, "flaky", m.tip):
            return
        link = e2e.run.html_url if e2e.run and e2e.run.html_url else ""
        body = (
            f"{marker('flaky', m.tip)}\n"
            f"**Merge train:** E2E على batch {m.label} (`{m.tip[:7]}`) فشل مرة ونجح في الإعادة: flaky.\n\n"
            f"الاختبارات اللي فشلت أول مرة:\n{self._tests_block(self.failing_tests(m.tip))}\n\n"
            + (f"الـ run: {link}\n\n" if link else "")
            + "مطلوب تذكرة flake لـ Salma. الـ batch كمل عادي."
        )
        self.comment(first, body, f"flaky batch {m.label}")

    def announce(self, m: Manifest, green: Sequence[Member], merged: int, e2e: E2EState, flaky: bool) -> None:
        """D5: «جاهز للدمج» for each green member not merged yet, in order, once per head and batch."""
        run_url = e2e.run.html_url if e2e.run else ""
        for index in range(merged, len(green)):
            mem = green[index]
            key = ready_key(mem, m)
            if self.said(mem.number, "ready", key):
                continue
            pr = self.pull(mem.number)
            if pr is None:
                continue
            passed = ["CI", "E2E", "Owner review"] + ([REVIEW_LABEL] if REVIEW_LABEL in pr.labels else [])
            note = _not_covered_note(self.gh, pr, self.checks(mem.sha), self.options)
            body = batch_ready_comment(pr, m, green, index, passed, note, flaky=flaky, run_url=run_url)
            self.comment(mem.number, body, f"ready ({index + 1}/{len(green)} of batch {m.label})")

    def tree_check(self, m: Manifest, green: Sequence[Member], end: int, path: Sequence[MainStep]) -> None:
        """D8: main's tree right after the last green member merged against the tree that was tested.
        A difference is reported, never acted on: the train dispatches nothing on main (Yousef, record
        5d9d0b13); the nightly E2E on main is the backstop, and Ahmed or an engineer runs E2E by hand."""
        if end < 0 or end >= len(path):
            return
        main_commit = path[end].sha
        tested = m.trees[len(green) - 1]
        actual = self.gh.tree_sha(main_commit)
        if actual == tested:
            self.report.did(f"tree check: main at {main_commit[:7]} has the tree batch {m.label} tested")
            return
        last = green[-1].number
        self.report.did(
            f"ATTENTION: tree check: main at {main_commit[:7]} differs from the tested tree of batch {m.label}; "
            f"run E2E on main by hand (the nightly is the backstop)"
        )
        if self.said(last, "tree", main_commit):
            return
        body = (
            f"{marker('tree', main_commit)}\n"
            f"**Merge train:** فحص الشجرة بعد دمج batch {m.label}: شجرة `{self.base_branch}` عند "
            f"`{main_commit[:7]}` مختلفة عن الشجرة اللي اتختبرت (`{m.commits[len(green) - 1][:7]}`).\n\n"
            f"القطار لا يشغّل E2E على `{self.base_branch}`. الـ nightly E2E عليه هو الـ backstop؛ "
            f"والأفضل يشغّله أحمد أو مهندس يدويا دلوقتي (Actions ← E2E ← Run workflow على `{self.base_branch}`)."
        )
        self.comment(last, body, f"tree check of batch {m.label} differs")

    def build_next(self, facts: Mapping[int, Facts], batch_no: int) -> None:
        def changes_workflows(number: int) -> bool:
            return any(path.startswith(WORKFLOWS_DIR) for path in self.gh.pull_files(number))

        size = self.options.batch_size
        selection = select_batch(self.config.order, facts, self.base_branch, size, changes_workflows)
        for number, reason in selection.waiting:
            self.notes.setdefault(number, ("waiting", reason))
        if not selection.batch:
            self.report.did("no candidate for a batch (open, not draft, on main, run-e2e, CI and Owner review green)")
            return
        members = tuple(Member(pr.number, pr.head_sha) for pr in selection.batch)
        built = self.build(members, batch_no, self.main_sha, bisect=None)
        label = f"{self.wave}#{batch_no}"
        for i, mem in enumerate(built):
            self.notes[mem.number] = (f"batch {label} {i + 1}/{len(built)}", "built; E2E dispatched")
        if built:
            self.live_members, self.live_label = built, label
            self.report.head = built[0].number

    def build(
        self,
        members: Sequence[Member],
        batch_no: int,
        base: str,
        bisect: tuple[str, Sequence[Member]] | None,
    ) -> tuple[Member, ...]:
        """D2: point train/run at `base`, merge each pinned head in order, then dispatch E2E. A member that
        conflicts (409) leaves the batch with a comment and `train-conflict`; the others go on."""
        label = f"{self.wave}#{batch_no}"
        names = ", ".join(f"#{mem.number}" for mem in members)
        if self.options.dry_run:
            self.report.did(f"dry run: would build batch {label} on {base[:7]} with {names} and dispatch E2E")
            return tuple(members)
        self.gh.set_train_ref(base)
        tip = base
        built: list[Member] = []
        for mem in members:
            result: MergeResult = self.gh.merge_into_train(mem.sha, manifest_message(self.wave, batch_no, mem, bisect))
            if result.status == "merged":
                tip = result.sha
                built.append(mem)
                if bisect is None:
                    self.label(mem.number, CONFLICT_LABEL, add=False)
                    self.label(mem.number, RED_LABEL, add=False)
            elif result.status == "conflict":
                self.conflict(mem, tip, base, built, label)
            else:
                self.report.did(f"#{mem.number}: its head is already in {TRAIN_REF}; left out of batch {label}")
        if not built:
            self.report.did(f"batch {label}: no member merged cleanly; nothing to test")
            return ()
        self.gh.dispatch_e2e()
        self.report.did(
            f"built batch {label} on {base[:7]} with {', '.join(f'#{x.number}' for x in built)}; "
            f"dispatched e2e.yml on {TRAIN_REF}"
        )
        return tuple(built)

    def conflict(self, mem: Member, tip: str, base: str, earlier: Sequence[Member], label: str) -> None:
        key = f"{mem.sha[:7]}-{base[:7]}"
        self.notes[mem.number] = ("conflict", f"left batch {label}")
        if not self.said(mem.number, "conflict", key):
            files = self.conflict_files(tip, mem.sha)
            if files is None and tip != base:
                files = self.conflict_files(base, mem.sha)
            body = batch_conflict_comment(mem, self.base_branch, base, label, earlier, files, key)
            self.comment(mem.number, body, f"conflict in batch {label}")
        else:
            self.report.did(f"#{mem.number} still conflicts in the train (already reported)")
        self.label(mem.number, CONFLICT_LABEL, add=True)

    def plain_ready(self, facts: Mapping[int, Facts]) -> None:
        """A pull request without run-e2e needs no E2E: «جاهز للدمج» when CI and Owner review are green on
        its head and it is up to date with main (batch mode never updates a branch). While a batch is live,
        the comment says «بعد الـ batch الحالي» and names its members (Yousef, note 2): merging it first
        would move main by a commit that is not the next member, which drops the batch (D6)."""
        live = [mem.number for mem in self.live_members if (p := self.pull(mem.number)) is not None and not p.merged]
        after = ""
        if live:
            after = f"بعد الـ batch الحالي ({self.live_label}: {' ثم '.join(f'#{n}' for n in live)})"
        for number in self.config.order:
            if number in live:
                continue
            f = facts[number]
            if candidacy(f, self.base_branch) != PLAIN or f.pr is None:
                continue
            pr = f.pr
            ci_ok = f.ci == "ok" or (f.ci == "missing" and f.owner != "missing")
            if not (ci_ok and f.owner == "ok"):
                self.notes[number] = ("waiting", f"plain ready check: CI {f.ci}, Owner review {f.owner}")
                continue
            if self.said(number, "ready", pr.head_sha):
                self.notes[number] = ("ready", "already announced for this head")
                continue
            if self.gh.behind_by(self.base_branch, pr.head_sha) > 0:
                self.notes[number] = ("behind", "batch mode never updates a branch: update it by hand")
                continue
            passed = ["CI", "Owner review"] + ([REVIEW_LABEL] if REVIEW_LABEL in pr.labels else [])
            note = _not_covered_note(self.gh, pr, self.checks(pr.head_sha), self.options)
            if after:
                note += "\n\nدمجه قبل أعضاء الـ batch الحالي يلغي الـ batch ويحتاج E2E جديد."
            body = ready_comment(pr, self.base_branch, passed, note, after=after)
            self.comment(number, body, "ready (no E2E needed)" + (", after the live batch" if after else ""))
            self.notes[number] = ("ready", "plain ready check" + (f", after batch {self.live_label}" if after else ""))


def run_batch(
    gh: GitHub,
    config: Config,
    *,
    options: Options,
    conflict_files: Callable[[str, str], list[str] | None],
    now: Callable[[], float] = time.time,
) -> Report:
    return _BatchRun(gh, config, options, conflict_files, now).go()
