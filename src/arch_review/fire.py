"""T4 driven by GitHub events (BASITA-161, plan section 10.10): the end of the Architecture review job
starts the reviewer's review, or the PR author's rework, through a routine's `/fire` URL.

Two halves, so the routine tokens never sit beside the GitHub reads:
- `decide_fire` (pure) runs in the check step, which already holds the PR, its reviews and the check's
  decision, and writes its answer to a file (`python -m arch_review --t4-out`).
- `python -m arch_review.fire` runs in the next step, the only one with the routine tokens in its
  environment. It reads that file, checks every field again, claims (PR, SHA, kind) once on the PR, and
  POSTs fixed fields only (`KIND REPO PR SHA TIER ROUND`), never any text from the PR.

The loop guards of plan 10.10:
1. Events made with the job's GITHUB_TOKEN start no workflow (GitHub's rule, except workflow_dispatch
   and repository_dispatch, which nothing here sends). The check's own writes, the reviewer request and
   the claim comment, use that token, so the check never starts itself. A fired run writes as the
   agents' own identities (the reviewer's review, the author's push), which do start the check again:
   that is the next round, and guards 3 to 5 bound it.
2. Only a PR opened by one of `t4.agent_bots`, from a branch of the same repository. A fork's PR never
   fires; the workflow also skips the step for one, so its run never has the secret.
3. The review fires only when the reviewer has no review on the head commit. The rework fires only when
   his latest review requests changes on the head commit (the check's `changes` state).
4. At most `t4.max_rework_rounds` (2) reworks. Once the reviewer has requested changes on more commits
   than that, nothing fires and the check says "round cap, waiting for <owner>". A dismissed review no
   longer counts, so the owner can allow one more round by dismissing it.
5. A claim marker: one comment by github-actions[bot] on the PR lists each (kind, SHA) fired, and a
   second run on the same commit stops at once. Claim before fire: a run cancelled between them fires
   nothing, and pickup, the fallback, catches it. Only the job's token writes as github-actions[bot],
   and a forged row could only stop a fire, never start one. The Mac claims again in
   `pickup-event.sh`, so even a double fire starts the work once.
6. Caps (live, daily, `REVIEW_MAX_DAILY`) are pickup's: `pickup-event.sh` on the Mac applies them before
   the fired run starts work. Nothing here counts. The `/fire` API also limits each routine to 30 fires
   an hour.
7. The repo variable `T4_FIRE`: `off` (the default; the step does not run), `dry-run` (prints what it
   would fire, calls nothing) or `on`.

The `/fire` call follows Anthropic's "Trigger a routine through the API" page (experimental, read
03/11/2026): POST `https://api.anthropic.com/v1/claude_code/routines/<trig_ id>/fire`, `Authorization:
Bearer <the routine's token>`, `anthropic-version`, `anthropic-beta`, and a JSON body `{"text": ...}`. There is no
idempotency key, so nothing here retries: a fire with no answer may have started its run, and keeps its
claim.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .github import FetchError, GitHub
from .review import CHANGES_STATE, OWNER_STATE, WAITING, Decision, Review
from .rules import Classification, Rules, RulesError

FIRE_MODES = ("off", "dry-run", "on")
KINDS = ("review", "rework")
TIERS = ("high", "xhigh", "max")
# The only fields a fire carries, each checked again right before the call.
FIELDS = {
    "KIND": re.compile(r"^(?:review|rework)$"),
    "REPO": re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$"),
    "PR": re.compile(r"^[1-9][0-9]{0,8}$"),
    "SHA": re.compile(r"^[0-9a-f]{40}$"),
    "TIER": re.compile(r"^(?:high|xhigh|max)$"),
    "ROUND": re.compile(r"^[1-9][0-9]?$"),
}
ROUTINE_NAME = re.compile(r"^[a-z]{1,20}$")
TRIGGER_ID = re.compile(r"^trig_[A-Za-z0-9]{1,64}$")
FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{}/fire"
API_VERSION = "2023-06-01"  # the endpoint accepts every version from this one on
# The routines API "ships under" this beta header; without it the fire is refused.
ROUTINE_BETA = "experimental-cc-routine-2026-04-01"
ACTIONS_BOT = "github-actions[bot]"
MARKER = "<!-- arch-review-t4 -->"
ROW = re.compile(r"<!-- t4 (review|rework) ([0-9a-f]{40}) (claimed|fired|failed) -->")
HEADER = (
    f"{MARKER}\n"
    "**Architecture review started these runs** (T4, BASITA-161). Each commit starts at most one review "
    "and one rework from here; pickup on the Mac claims each run again.\n\n"
    "| Kind | Commit | Round | Tier | When (UTC) | Result |\n"
    "|---|---|---|---|---|---|"
)


class FireError(ValueError):
    """A fire decision or its fields are not what this module writes."""


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


@dataclass(frozen=True)
class FireRules:
    """The `architecture_review.t4` block of the rules file, read from the base branch.
    Without it, `agent_bots` is empty and nothing ever fires."""

    agent_bots: frozenset[str] = field(default_factory=frozenset)
    max_rework_rounds: int = 2
    review_routine: str = "opus"
    rework_routine: Mapping[str, str] = field(default_factory=dict)  # tier -> routine; unlisted is opus

    @classmethod
    def from_json(cls, data: dict) -> FireRules:
        arch = (data or {}).get("architecture_review") if isinstance(data, dict) else None
        t4 = arch.get("t4") if isinstance(arch, dict) else None
        if t4 is None:
            return cls()
        if not isinstance(t4, dict):
            raise RulesError("'architecture_review.t4' must be an object")
        bots = t4.get("agent_bots", [])
        if not isinstance(bots, list) or not all(isinstance(b, str) and b for b in bots):
            raise RulesError("'t4.agent_bots' must be a list of GitHub logins")
        rounds = t4.get("max_rework_rounds", 2)
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 0 <= rounds <= 9:
            raise RulesError("'t4.max_rework_rounds' must be a whole number from 0 to 9")
        review = t4.get("review_routine", "opus")
        rework = t4.get("rework_routine", {})
        if not isinstance(review, str) or not ROUTINE_NAME.fullmatch(review):
            raise RulesError("'t4.review_routine' must be a routine name such as opus")
        if not isinstance(rework, dict) or not all(
            t in TIERS and isinstance(r, str) and ROUTINE_NAME.fullmatch(r) for t, r in rework.items()
        ):
            raise RulesError("'t4.rework_routine' must map high, xhigh or max to a routine name such as sonnet")
        return cls(frozenset(b.lower() for b in bots), rounds, review, dict(rework))

    def routine(self, kind: str, tier: str) -> str:
        return self.review_routine if kind == "review" else self.rework_routine.get(tier, "opus")


@dataclass(frozen=True)
class Fire:
    """What T4 does for one check run: fire `kind`, or nothing (`kind` empty) with the reason."""

    reason: str
    kind: str = ""
    repo: str = ""
    pr: int = 0
    sha: str = ""
    tier: str = ""
    round: int = 0
    routine: str = ""
    capped: bool = False

    def fields(self) -> list[tuple[str, str]]:
        """The fixed fields, each checked. Raises FireError on any that isn't what decide_fire writes."""
        if not self.kind:
            raise FireError("nothing to fire")
        pairs = [
            ("KIND", self.kind),
            ("REPO", self.repo),
            ("PR", str(self.pr)),
            ("SHA", self.sha),
            ("TIER", self.tier),
            ("ROUND", str(self.round)),
        ]
        for name, value in pairs:
            if not FIELDS[name].fullmatch(value):
                raise FireError(f"{name} is not a valid value")
        if not ROUTINE_NAME.fullmatch(self.routine):
            raise FireError("the routine name is not valid")
        return pairs

    def text(self) -> str:
        """The `/fire` text: `KIND=review REPO=o/r PR=12 SHA=<40 hex> TIER=max ROUND=1`, nothing else."""
        return " ".join(f"{k}={v}" for k, v in self.fields())

    def describe(self) -> str:
        if not self.kind:
            return self.reason
        return f"{self.kind} of {self.sha[:7]}, round {self.round}, tier {self.tier}, {self.routine} routine"

    def to_json(self) -> dict:
        return {
            "reason": self.reason,
            "kind": self.kind,
            "repo": self.repo,
            "pr": self.pr,
            "sha": self.sha,
            "tier": self.tier,
            "round": self.round,
            "routine": self.routine,
            "capped": self.capped,
        }

    @classmethod
    def from_json(cls, data: object) -> Fire:
        """Read the check step's file strictly; a fire with any bad field raises FireError."""
        if not isinstance(data, dict):
            raise FireError("the decision file holds no object")
        kind = data.get("kind") or ""
        pr, rnd = data.get("pr", 0), data.get("round", 0)
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (pr, rnd)):
            raise FireError("pr and round must be whole numbers")
        fire = cls(
            reason=_one_line(data.get("reason") or "")[:300],
            kind=kind if isinstance(kind, str) else "?",
            repo=str(data.get("repo") or ""),
            pr=pr,
            sha=str(data.get("sha") or ""),
            tier=str(data.get("tier") or ""),
            round=rnd,
            routine=str(data.get("routine") or ""),
            capped=data.get("capped") is True,
        )
        if fire.kind:
            fire.fields()
        return fire


def change_rounds(reviews: list[Review], reviewer: str) -> int:
    """How many commits the reviewer has requested changes on (a dismissed request no longer counts)."""
    return len({r.commit_id for r in reviews if r.login.lower() == reviewer.lower() and r.state == "CHANGES_REQUESTED"})


def decide_fire(
    rules: Rules,
    t4: FireRules,
    repo: str,
    pr: dict,
    classification: Classification,
    decision: Decision,
    reviews: list[Review],
    head_sha: str,
) -> Fire:
    """Whether this check run fires the reviewer's review, the author's rework, or nothing (guards 2 to 4)."""
    if not classification.required or decision.green:
        return Fire("nothing to start: " + ("no review needed" if not classification.required else "approved"))
    if pr.get("state", "open") != "open":
        return Fire("nothing to start: the PR is not open")
    if pr.get("draft"):
        return Fire("nothing to start: draft")
    head_repo = (((pr.get("head") or {}).get("repo") or {}).get("full_name") or "").lower()
    if head_repo != repo.lower():
        return Fire("never fires: the PR's branch is not in this repository (a fork)")
    fresh = ((pr.get("head") or {}).get("sha") or "").lower()
    if fresh and fresh != head_sha.lower():
        # Two quick pushes would otherwise start two reviews: only the run on the newest head may fire.
        return Fire(f"nothing to start: the PR moved on to {fresh[:7]}; the run on the new head decides")
    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author not in t4.agent_bots:
        return Fire(f"nothing to start: opened by {author or 'unknown'}, not an agent's bot in t4.agent_bots")
    if decision.state == OWNER_STATE:
        return Fire(f"nothing to start: {rules.owner}'s turn")

    rounds = change_rounds(reviews, rules.reviewer)
    if rounds > t4.max_rework_rounds:
        return Fire(
            f"round cap, waiting for {rules.owner}: {rules.reviewer} requested changes on {rounds} commits "
            f"(at most {t4.max_rework_rounds} reworks); nothing fires",
            capped=True,
        )
    tier = classification.effort
    base = {"repo": repo, "pr": int(pr.get("number") or 0), "sha": head_sha, "tier": tier}
    if decision.state == WAITING:
        if any(r.login.lower() == rules.reviewer.lower() and r.commit_id == head_sha for r in reviews):
            return Fire(f"nothing to start: {rules.reviewer} already reviewed {head_sha[:7]}")
        kind, number = "review", rounds + 1
    elif decision.state == CHANGES_STATE:
        kind, number = "rework", rounds
    else:
        return Fire(f"nothing to start: state {decision.state}")
    fire = Fire(f"start the {kind}", kind=kind, round=number, routine=t4.routine(kind, tier), **base)
    try:
        fire.fields()
    except FireError as exc:
        return Fire(f"nothing to start: {exc}")
    return fire


# --- the fire step -----------------------------------------------------------------------------------


def parse_routines(spec: str) -> dict[str, str]:
    """`opus=trig_…,sonnet=trig_…` (the repo variable T4_ROUTINES) to {name: trigger id}; bad pairs are dropped."""
    out = {}
    for pair in (spec or "").replace(";", ",").split(","):
        name, _, trigger = pair.strip().partition("=")
        if ROUTINE_NAME.fullmatch(name.strip()) and TRIGGER_ID.fullmatch(trigger.strip()):
            out[name.strip()] = trigger.strip()
    return out


def find_marker(comments: list[dict]) -> tuple[int, str] | None:
    """The claim comment: the first one by github-actions[bot] that starts with the marker."""
    for c in comments:
        body = c.get("body") or ""
        if (c.get("user") or {}).get("login") == ACTIONS_BOT and body.startswith(MARKER) and c.get("id"):
            return int(c["id"]), body
    return None


def claimed(body: str, kind: str, sha: str) -> bool:
    """Whether (kind, sha) is claimed or fired; the last row for it wins, and a failed fire may run again."""
    status = ""
    for m in ROW.finditer(body or ""):
        if m.group(1) == kind and m.group(2) == sha:
            status = m.group(3)
    return status in ("claimed", "fired")


def with_row(body: str, fire: Fire, status: str, result: str, now: str) -> str:
    """The claim comment with (kind, sha)'s row set to `status`, replaced in place or added."""
    tag = f"<!-- t4 {fire.kind} {fire.sha} "
    cell = _one_line(result).replace("|", "/")[:120]
    line = f"| {fire.kind} | {fire.sha[:7]} | {fire.round} | {fire.tier} | {now} | {cell} | {tag}{status} -->"
    lines = (body or HEADER).splitlines()
    if any(tag in old for old in lines):
        lines = [line if tag in old else old for old in lines]
    else:
        lines.append(line)
    return "\n".join(lines)


def http_post(url: str, headers: dict[str, str], body: bytes, timeout: float = 20) -> tuple[int, str]:
    """POST and return (status, start of the answer); status 0 when there was no answer."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - fixed https URL
            return answer.status, answer.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return 0, type(exc).__name__


def send_fire(
    fire: Fire, trigger: str, token: str, post: Callable[..., tuple[int, str]] = http_post
) -> tuple[bool | None, str]:
    """POST the fixed fields. Returns (True, session id), (False, why) when it surely did not start, or
    (None, why) when it may have started (no answer). The token goes in the header only, never in a message."""
    body = json.dumps({"text": fire.text()}).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": API_VERSION,
        "anthropic-beta": ROUTINE_BETA,
        "Content-Type": "application/json",
    }
    status, answer = post(FIRE_URL.format(trigger), headers, body)
    try:
        data = json.loads(answer)
    except (json.JSONDecodeError, TypeError):
        data = {}
    data = data if isinstance(data, dict) else {}
    if status == 200:
        session = str(data.get("claude_code_session_id") or "")
        return True, session if re.fullmatch(r"^[A-Za-z0-9_-]{1,100}$", session) else "unknown session"
    if status == 0:
        name = answer if re.fullmatch(r"^[A-Za-z]{1,40}$", answer or "") else "error"
        return None, f"no answer ({name}); the run may have started, so it keeps its claim"
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    kind = re.sub(r"[^a-z_]", "", str(error.get("type") or ""))[:40]
    return False, f"HTTP {status}" + (f" {kind}" if kind else "")


def _annotation(kind: str, message: str) -> str:
    message = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{kind} title=T4 fire::{message}"


def run_fire(
    fire: Fire,
    mode: str,
    routines: dict[str, str],
    env: Mapping[str, str],
    gh: GitHub,
    post: Callable[..., tuple[int, str]] = http_post,
    now: Callable[[], str] = lambda: time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
) -> list[str]:
    """Do what the mode says with one decision. Returns the lines to print; never raises for GitHub."""
    if mode == "off":
        return ["T4 is off (T4_FIRE=off): nothing fired."]
    if not fire.kind:
        return [_annotation("notice", fire.reason)]
    text = fire.text()
    if mode == "dry-run":
        return [_annotation("notice", f"dry-run: would fire the {fire.routine} routine with: {text}")]

    trigger = routines.get(fire.routine, "")
    secret = f"ROUTINE_FIRE_{fire.routine.upper()}"
    token = env.get(secret) or ""
    if not trigger:
        return [_annotation("warning", f"no trigger id for the {fire.routine} routine in T4_ROUTINES: nothing fired")]
    if not token or not re.fullmatch(r"^[\x21-\x7e]{20,400}$", token):
        return [_annotation("warning", f"the secret {secret} is missing or malformed: nothing fired")]

    issue = f"repos/{fire.repo}/issues/{fire.pr}/comments"
    try:
        marker = find_marker(gh.items(issue))
    except FetchError as exc:
        return [_annotation("warning", f"could not read the claim comment ({_one_line(exc)[:150]}): nothing fired")]
    if marker and claimed(marker[1], fire.kind, fire.sha):
        return [f"T4: the {fire.kind} of {fire.sha[:7]} was already started from GitHub; nothing fired."]

    body = with_row(marker[1] if marker else "", fire, "claimed", "claimed", now())
    if marker:
        _, error = gh.send("PATCH", f"repos/{fire.repo}/issues/comments/{marker[0]}", [f"body={body}"])
        comment_id = marker[0]
    else:
        answer, error = gh.send("POST", issue, [f"body={body}"])
        comment_id = int((answer or {}).get("id") or 0)
    if error:
        return [_annotation("warning", f"could not claim the {fire.kind} ({_one_line(error)[:150]}): nothing fired")]

    started, detail = send_fire(fire, trigger, token, post)
    status = {True: "fired", False: "failed", None: "claimed"}[started]
    result = {True: "fired", False: f"fire failed: {detail}", None: "no answer; may have started"}[started]
    lines = []
    if comment_id:
        _, error = gh.send(
            "PATCH",
            f"repos/{fire.repo}/issues/comments/{comment_id}",
            [f"body={with_row(body, fire, status, result, now())}"],
        )
        if error:
            lines.append(
                _annotation("warning", f"fired, but could not update the claim comment: {_one_line(error)[:150]}")
            )
    if started:
        lines.insert(
            0, _annotation("notice", f"fired the {fire.routine} routine ({fire.describe()}): session {detail}")
        )
    elif started is None:
        lines.insert(0, _annotation("warning", f"{fire.kind} of {fire.sha[:7]}: {detail}"))
    else:
        lines.insert(
            0,
            _annotation(
                "warning", f"{fire.kind} of {fire.sha[:7]} not started: {detail}; a later event or pickup retries"
            ),
        )
    return lines


def main(
    argv: list[str] | None = None,
    gh: GitHub | None = None,
    env: Mapping[str, str] | None = None,
    post: Callable[..., tuple[int, str]] = http_post,
) -> int:
    parser = argparse.ArgumentParser(prog="arch-review-fire", description="T4: start a review or rework run.")
    parser.add_argument("--decision", required=True, help="the file the check step wrote with --t4-out")
    parser.add_argument("--mode", default="", help="off, dry-run or on (T4_FIRE; empty means off)")
    parser.add_argument("--routines", default="", help="opus=trig_…,sonnet=trig_… (T4_ROUTINES)")
    parser.add_argument("--summary", help="file to append the Markdown summary to ($GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    mode = (args.mode or "off").strip().lower()
    if mode not in FIRE_MODES:
        lines = [
            _annotation("warning", f"T4_FIRE is '{_one_line(args.mode)[:40]}'; use off, dry-run or on. Nothing fired.")
        ]
    elif mode == "off":
        lines = ["T4 is off (T4_FIRE=off): nothing fired."]
    else:
        try:
            fire = Fire.from_json(json.loads(Path(args.decision).read_text(encoding="utf-8")))
        except FileNotFoundError:
            fire = Fire("nothing to start: the check reached no decision")
        except (OSError, ValueError) as exc:  # FireError and JSONDecodeError are ValueErrors
            fire = Fire(f"nothing to start: the decision file is not valid ({_one_line(exc)[:100]})")
        try:
            lines = run_fire(fire, mode, parse_routines(args.routines), env, gh or GitHub(), post)
        except Exception as exc:  # noqa: BLE001 - a T4 problem must never turn the check red
            lines = [_annotation("warning", f"unexpected {type(exc).__name__}: nothing more fired")]
    for line in lines:
        print(line)
    if args.summary:
        with Path(args.summary).open("a", encoding="utf-8") as out:
            shown = [re.sub(r"^::\w+ title=T4 fire::", "", line).replace("%0A", " ") for line in lines]
            out.write("\n".join(["### T4 fire", "", f"Mode: {mode}", "", *[f"- {s}" for s in shown], ""]) + "\n")
    return 0  # the check step's result is the check's; T4 never changes it


if __name__ == "__main__":
    raise SystemExit(main())
