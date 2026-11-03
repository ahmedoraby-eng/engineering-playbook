"""Talia's merge on Ahmed's delegation (BASITA-162, plan 10.9, decisions D22 and D23).

    agent-gh talia pr merge <N> [-R owner/repo]    the checks, then a squash merge with the head pinned
    agent-gh talia merge-check <N> [-R owner/repo] the same checks, and nothing is merged

A delegation is valid only as an issue in basitasolutions/basita-ops with the label `delegation`,
opened by Ahmed (`ahmedoraby-eng`), or by `basita-sync[bot]` when Talia records his words from a session
on his Mac (`delegation-record.sh`, D22). Its title is "Delegation until HH:MM" (Cairo time) or
"Delegation until YYYY-MM-DD HH:MM". It starts when GitHub opened it and ends at that time, at most 12
hours later, or when the issue is first closed, whichever comes first; reopening it doesn't bring it back.
A title renamed by anyone but Ahmed voids it. "Now" is GitHub's clock (the Date header of its answers), not the Mac's.

Before each merge, everything is read fresh with the read-only App (reader), and every point must hold:
1. a valid delegation now;
2. the PR is open, targets main, isn't a draft and can merge without a conflict;
3. every check run on the head commit has finished green (success, neutral or skipped), Architecture
   review is among them and is success, and every commit status on the head is success;
4. no file (old or new name) is on a critical path of the repository's `.github/review-rules.json`, read
   from main: those wait for Ahmed. A PR GitHub lists fewer files for than it changed counts as critical;
5. D23: on a repository whose rules have `jira_status` (BASITA-168), the `Jira status` status on the head
   is success and was posted by github-actions[bot], and Jira is read again here, each DFP/ELB key in
   the title or branch in one of its project's statuses. A PR with the owner's waiver label waits for him;
6. the PR isn't Talia's own: not opened by her bot, and no commit by it.
D23 "all repositories": basita-ops, elbayoumy and the playbook. Talia's App must be installed on the
owner, which the playbook (ahmedoraby-eng) needs Ahmed for.

The merge is a squash with `sha` set to the checked head, so a push in between makes GitHub refuse it.
Its commit message names the delegation issue. Then the PR gets the label `merged-on-delegation` and a
comment. Nothing here prints a token.
"""

from __future__ import annotations

import base64
import datetime as dt
import email.utils
import json
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from arch_review.rules import commit_logins

from .identity import IdentityError, split_repo

if TYPE_CHECKING:
    from collections.abc import Callable

    # (method, path, body) -> (headers, payload), already authorised as one identity.
    Call = Callable[[str, str, dict | None], tuple[dict, object]]

AHMED = "ahmedoraby-eng"
RECORDER = "basita-sync[bot]"
TALIA = "basita-talia[bot]"
JIRA_STATUS_BOT = "github-actions[bot]"
DELEGATION_REPO = "basitasolutions/basita-ops"
LABEL = "delegation"
MERGED_LABEL = "merged-on-delegation"
REPOS = (
    "basitasolutions/basita-ops",
    "basitasolutions/elbayoumy-farm-nutrition-platform",
    "ahmedoraby-eng/engineering-playbook",
)
ARCHITECTURE_REVIEW = "Architecture review"
GREEN = {"success", "neutral", "skipped"}
MAX_WINDOW = dt.timedelta(hours=12)
UTC = dt.timezone.utc  # noqa: UP017 - the CLI runs on macOS's system Python 3.9, which has no dt.UTC
CAIRO_WINTER, CAIRO_SUMMER = dt.timedelta(hours=2), dt.timedelta(hours=3)
UNTIL = re.compile(r"^Delegation until (?:(\d{4}-\d{2}-\d{2}) )?([01]?\d|2[0-3]):([0-5]\d)\s*$")
KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def cairo_offset(when: dt.datetime) -> dt.timedelta:
    """Egypt's UTC offset at a moment: +3 from the last Friday of April to the last Thursday of October
    (summer time since 2023), +2 otherwise. Kept here, not in zoneinfo, because the Mac's system Python
    3.9 may have no tz database. The switch hour is taken as midnight, close enough for a 12-hour cap."""
    year = when.year

    def last(month: int, weekday: int) -> dt.date:
        day = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
        return day - dt.timedelta(days=(day.weekday() - weekday) % 7)

    start, end = last(4, 4), last(10, 3) + dt.timedelta(days=1)
    return CAIRO_SUMMER if start <= (when + CAIRO_WINTER).date() < end else CAIRO_WINTER


def parse_until(title: str, opened: dt.datetime) -> dt.datetime | None:
    """The end the title names, in UTC, or None when the title isn't a delegation's. "HH:MM" is the first
    such Cairo time after the issue was opened."""
    match = UNTIL.match((title or "").strip())
    if not match:
        return None
    day, hour, minute = match.group(1), int(match.group(2)), int(match.group(3))
    offset = cairo_offset(opened)
    local_open = opened + offset
    if day:
        try:
            local = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hour, minute), tzinfo=UTC)
        except ValueError:
            return None
    else:
        local = local_open.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local <= local_open:
            local += dt.timedelta(days=1)
    return local - offset


def _time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class Delegation:
    number: int
    url: str
    opened_by: str
    start: dt.datetime
    end: dt.datetime


def delegation_window(issue: dict, events: list[dict]) -> tuple[Delegation | None, str]:
    """The window of one `delegation` issue, or None and why it doesn't count."""
    number = issue.get("number")
    opener = ((issue.get("user") or {}).get("login") or "").lower()
    if "pull_request" in issue:
        return None, f"#{number} is a pull request"
    if opener not in (AHMED, RECORDER):
        return None, f"#{number} was opened by {opener or 'nobody'}, not Ahmed or basita-sync[bot] on his Mac"
    if LABEL not in {(label.get("name") or "").lower() for label in issue.get("labels") or []}:
        return None, f"#{number} has no '{LABEL}' label"
    renamers = sorted(
        {
            ((e.get("actor") or {}).get("login") or "?").lower()
            for e in events
            if e.get("event") == "renamed" and ((e.get("actor") or {}).get("login") or "").lower() != AHMED
        }
    )
    if renamers:
        return None, f"#{number} was renamed by {', '.join(renamers)}"
    opened = _time(issue.get("created_at"))
    until = parse_until(issue.get("title", ""), opened) if opened else None
    if not until:
        return None, f"#{number}'s title isn't 'Delegation until HH:MM'"
    end = min(until, opened + MAX_WINDOW)
    # The first close ends it for good: GitHub clears closed_at when an issue is reopened, so the events decide,
    # and a reopen never brings a delegation back.
    closes = [_time(e.get("created_at")) for e in events if e.get("event") == "closed" and e.get("created_at")]
    closed = _time(issue.get("closed_at"))
    for moment in [*closes, closed]:
        if moment:
            end = min(end, moment)
    return Delegation(number, issue.get("html_url", ""), opener, opened, end), ""


def delegations(read: Call, since: dt.datetime) -> tuple[list[Delegation], list[str]]:
    """Every `delegation` issue in basita-ops updated since `since`, open or closed, with why the others
    don't count."""
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    _, issues = read(
        "GET", f"/repos/{DELEGATION_REPO}/issues?labels={LABEL}&state=all&since={stamp}&per_page=100", None
    )
    found, skipped = [], []
    for issue in issues or []:
        _, events = read("GET", f"/repos/{DELEGATION_REPO}/issues/{issue['number']}/events?per_page=100", None)
        window, why = delegation_window(issue, events or [])
        if window:
            found.append(window)
        else:
            skipped.append(why)
    return found, skipped


def covering(read: Call, when: dt.datetime) -> Delegation | None:
    """The delegation whose window holds `when`, if any (for main-guard-check.sh)."""
    found, _ = delegations(read, when - MAX_WINDOW - dt.timedelta(minutes=5))
    return next((d for d in found if d.start <= when < d.end), None)


def github_now(headers: dict) -> dt.datetime:
    for name, value in (headers or {}).items():
        if name.lower() == "date":
            return email.utils.parsedate_to_datetime(value).astimezone(UTC)
    raise IdentityError("GitHub's answer had no Date header, so the delegation window can't be checked")


@dataclass
class Checked:
    repo: str
    number: int
    title: str
    head: str
    delegation: Delegation | None = None
    failures: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)


def _pages(read: Call, path: str, key: str | None = None) -> list:
    items: list = []
    page = 1
    while True:
        _, payload = read("GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}", None)
        batch = (payload or {}).get(key, []) if key else (payload or [])
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def _rules(read: Call, repo: str) -> dict:
    _, payload = read("GET", f"/repos/{repo}/contents/.github/review-rules.json?ref=main", None)
    try:
        return json.loads(base64.b64decode((payload or {}).get("content", "")))
    except ValueError:
        raise IdentityError(f"{repo}'s .github/review-rules.json on main can't be read") from None


def _pattern(parts) -> re.Pattern | None:
    parts = [parts] if isinstance(parts, str) else parts or []
    return re.compile("|".join(f"(?:{p})" for p in parts)) if parts else None


def check_merge(
    repo: str,
    number: int,
    read: Call,
    jira: Callable[[str], str],
    sleep: Callable[[float], None] = time.sleep,
) -> Checked:
    """Every point of the module docstring, read fresh. `jira(key)` returns an issue's status name."""
    if repo.lower() not in REPOS:
        raise IdentityError(f"refused: a delegated merge works on {', '.join(REPOS)} only, not {repo}")
    split_repo(repo)
    headers, pr = read("GET", f"/repos/{repo}/pulls/{number}", None)
    for _ in range(3):  # GitHub computes `mergeable` in the background after a push
        if pr.get("mergeable") is not None:
            break
        sleep(2)
        headers, pr = read("GET", f"/repos/{repo}/pulls/{number}", None)
    now = github_now(headers)
    head = (pr.get("head") or {}).get("sha", "")
    result = Checked(repo, number, pr.get("title", ""), head)
    fail, ok = result.failures.append, result.passed.append

    # 1. A valid delegation now.
    found, skipped = delegations(read, now - MAX_WINDOW - dt.timedelta(minutes=5))
    current = [d for d in found if d.start <= now < d.end]
    if current:
        result.delegation = max(current, key=lambda d: d.end)
        ok(f"delegation {result.delegation.url} until {result.delegation.end:%Y-%m-%d %H:%M} UTC")
    else:
        why = "; ".join(skipped) if skipped else "none open in the last 12 hours"
        fail(f"no valid delegation now ({why})")

    # 2. The PR itself.
    state = []
    if pr.get("state") != "open":
        state.append(f"it is {pr.get('state')}")
    if (pr.get("base") or {}).get("ref") != "main":
        state.append(f"it targets {(pr.get('base') or {}).get('ref')}, not main")
    if pr.get("draft"):
        state.append("it is a draft")
    if pr.get("mergeable") is not True or pr.get("mergeable_state") == "dirty":
        state.append(f"GitHub can't merge it cleanly (mergeable {pr.get('mergeable')}, {pr.get('mergeable_state')})")
    if state:
        fail("the PR: " + "; ".join(state))
    else:
        ok("the PR is open, targets main, isn't a draft and has no conflict")

    # 3. Checks and statuses on the head.
    runs = _pages(read, f"/repos/{repo}/commits/{head}/check-runs?filter=latest", "check_runs")
    not_green = sorted(
        f"{r.get('name')} ({r.get('conclusion') or r.get('status')})"
        for r in runs
        if r.get("status") != "completed" or r.get("conclusion") not in GREEN
    )
    review = [r for r in runs if r.get("name") == ARCHITECTURE_REVIEW]
    _, combined = read("GET", f"/repos/{repo}/commits/{head}/status", None)
    statuses = (combined or {}).get("statuses") or []
    not_green += sorted(f"{s.get('context')} ({s.get('state')})" for s in statuses if s.get("state") != "success")
    if not_green:
        fail(f"not green on {head[:7]}: {', '.join(not_green)}")
    if not review or any(r.get("conclusion") != "success" for r in review):
        fail(f"{ARCHITECTURE_REVIEW} is not success on {head[:7]}")
    if not not_green and review and all(r.get("conclusion") == "success" for r in review):
        ok(f"{len(runs)} check runs and {len(statuses)} statuses green on {head[:7]}, {ARCHITECTURE_REVIEW} included")

    # 4. Critical paths, from the rules on main.
    rules = _rules(read, repo)
    critical = _pattern(rules.get("critical_paths"))
    files = _pages(read, f"/repos/{repo}/pulls/{number}/files")
    names = sorted({n for f in files for n in (f.get("filename"), f.get("previous_filename")) if n})
    if critical is None:
        fail(f"{repo}'s review rules list no critical paths, so nothing can be merged on a delegation")
    elif len(files) < int(pr.get("changed_files") or 0):
        fail(f"GitHub listed {len(files)} of {pr.get('changed_files')} files: counts as critical, waits for Ahmed")
    else:
        hits = [n for n in names if critical.search(n)]
        if hits:
            fail(f"critical paths, these wait for Ahmed: {', '.join(hits)}")
        else:
            ok(f"no critical path among {len(names)} files")

    # 5. Jira status (D23), only where the repository has the BASITA-168 check.
    labels = {(label.get("name") or "").lower() for label in pr.get("labels") or []}
    jira_rules = rules.get("jira_status")
    if isinstance(jira_rules, dict):
        context = jira_rules.get("context", "Jira status")
        mine = [s for s in statuses if s.get("context") == context]
        waiver = (jira_rules.get("waiver_label") or "").lower()
        if waiver and waiver in labels:
            fail(f"'{waiver}' is on the PR: Ahmed waived the Jira status, so he merges it")
        elif not mine:
            fail(f"no '{context}' status on {head[:7]}")
        elif mine[0].get("state") != "success" or (mine[0].get("creator") or {}).get("login") != JIRA_STATUS_BOT:
            by = (mine[0].get("creator") or {}).get("login")
            fail(f"'{context}' on {head[:7]} is {mine[0].get('state')} by {by}, not success by {JIRA_STATUS_BOT}")
        else:
            projects = jira_rules.get("projects") or {}
            keys = sorted({k for k in KEY.findall(f"{pr.get('title', '')} {(pr.get('head') or {}).get('ref', '')}")})
            keys = [k for k in keys if k.split("-")[0] in projects]
            wrong = []
            for key in keys:
                status = jira(key)
                if status not in projects[key.split("-")[0]]:
                    wrong.append(f"{key} is {status}")
            if wrong:
                fail(f"Jira, read again now: {', '.join(wrong)}")
            else:
                ok(f"'{context}' success by {JIRA_STATUS_BOT}, and Jira read again: {', '.join(keys) or 'no key'}")
    else:
        ok("no Jira status check on this repository (D23: no other Jira condition)")

    # 6. Not Talia's own PR.
    commits = _pages(read, f"/repos/{repo}/pulls/{number}/commits")
    if ((pr.get("user") or {}).get("login") or "").lower() == TALIA or any(TALIA in commit_logins(c) for c in commits):
        fail("it is Talia's own PR (opened by or with a commit from basita-talia[bot])")
    else:
        ok("not Talia's own PR")
    return result


def merge(checked: Checked, write: Call) -> dict:
    """The squash merge with the checked head pinned, then the trace. Returns GitHub's merge answer."""
    if checked.failures or not checked.delegation:
        raise IdentityError("refused: " + "; ".join(checked.failures or ["no valid delegation"]))
    repo, number = checked.repo, checked.number
    message = (
        f"Merged by Talia on Ahmed's delegation: {checked.delegation.url}\n\n"
        f"Checked head {checked.head}; checks: " + "; ".join(checked.passed)
    )
    _, answer = write(
        "PUT",
        f"/repos/{repo}/pulls/{number}/merge",
        {
            "merge_method": "squash",
            "sha": checked.head,
            "commit_title": f"{checked.title} (#{number})",
            "commit_message": message,
        },
    )
    return answer


def trace(checked: Checked, answer: dict, write: Call) -> list[str]:
    """The label and the comment on the merged PR. A failure here doesn't undo the merge; it is reported."""
    notes = []
    repo, number = checked.repo, checked.number
    body = (
        f"Merged by Talia (basita-talia[bot]) on Ahmed's delegation {checked.delegation.url}, "
        f"squash {str(answer.get('sha', ''))[:7]} of the checked head {checked.head[:7]}.\n\n"
        + "\n".join(f"- {line}" for line in checked.passed)
    )
    for method, path, payload, what in (
        ("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": [MERGED_LABEL]}, f"label {MERGED_LABEL}"),
        ("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}, "comment"),
    ):
        try:
            write(method, path, payload)
            notes.append(f"ok {what}")
        except IdentityError as error:
            notes.append(f"attention {what} not added: {error}")
    return notes


# --- the command line (agent-gh talia pr merge|merge-check, agent-gh reader delegation-covers) ---------------

MERGE_WRITE = {"contents": "write", "pull_requests": "write", "metadata": "read"}
USAGE = (
    "usage: agent-gh talia pr merge <N> [-R owner/repo] | agent-gh talia merge-check <N> [-R owner/repo]"
    " | agent-gh reader delegation-covers <ISO time>"
    " | agent-gh sync delegation-record --until HH:MM < words | agent-gh sync delegation-note <issue> < text"
)
RECORD_WRITE = {"issues": "write", "metadata": "read"}


def is_delegation_command(agent: str, args: list[str]) -> bool:
    return (
        (agent == "talia" and (args[:2] == ["pr", "merge"] or args[:1] == ["merge-check"]))
        or (agent == "reader" and args[:1] == ["delegation-covers"])
        or (agent == "sync" and args[:1] in (["delegation-record"], ["delegation-note"]))
    )


def record_body(words: str) -> str:
    quoted = "\n".join(f"> {line}" if line else ">" for line in words.strip().splitlines())
    return (
        "Ahmed's words, in a session on his Mac, recorded by Talia with `delegation-record.sh` (D22):\n\n"
        f"{quoted}\n\nThe window starts when this issue was opened and ends at the time in its title, at most 12 hours "
        "later. Closing this issue ends it at once."
    )


def sync_command(args: list[str], stdin: str, read: Call, write: Call) -> str:
    """basita-sync on the Mac: open a delegation issue from Ahmed's words, or note a merge on one."""
    text = stdin.strip()
    if not text:
        raise IdentityError("refused: nothing on stdin (Ahmed's words, or the note)")
    if args[:1] == ["delegation-record"]:
        if len(args) != 3 or args[1] != "--until":
            raise IdentityError(USAGE)
        title = f"Delegation until {args[2]}"
        headers, _ = read("GET", f"/repos/{DELEGATION_REPO}", None)
        now = github_now(headers)
        until = parse_until(title, now)
        if not until:
            raise IdentityError(f"refused: '{args[2]}' is not HH:MM or YYYY-MM-DD HH:MM (Cairo time)")
        if not now < until <= now + MAX_WINDOW:
            raise IdentityError(f"refused: {args[2]} Cairo time is not within the next 12 hours")
        _, created = write(
            "POST", f"/repos/{DELEGATION_REPO}/issues", {"title": title, "body": record_body(text), "labels": [LABEL]}
        )
        return f"recorded {created.get('html_url')} until {until:%Y-%m-%d %H:%M} UTC"
    if len(args) != 2 or not args[1].isdigit():
        raise IdentityError(USAGE)
    _, target = read("GET", f"/repos/{DELEGATION_REPO}/issues/{args[1]}", None)
    if LABEL not in {(label.get("name") or "").lower() for label in target.get("labels") or []}:
        raise IdentityError(f"refused: #{args[1]} is not a delegation issue")
    write("POST", f"/repos/{DELEGATION_REPO}/issues/{args[1]}/comments", {"body": text})
    return f"noted on {target.get('html_url')}"


def _pr_args(args: list[str], default_repo: str | None) -> tuple[str, int]:
    rest, repo = list(args), default_repo
    for flag in ("-R", "--repo"):
        if flag in rest:
            at = rest.index(flag)
            if at + 1 >= len(rest):
                raise IdentityError(USAGE)
            repo = rest[at + 1]
            del rest[at : at + 2]
    if len(rest) != 1 or not rest[0].isdigit():
        raise IdentityError(f"refused: a delegated merge takes a PR number and -R only, no other flag. {USAGE}")
    if not repo:
        raise IdentityError("no repository: pass -R owner/repo")
    return repo, int(rest[0])


class _Tokens:
    """One installation token per repository and identity, minted when first needed, all revoked at the end."""

    def __init__(self, config: dict, transport, token_for, revoke):
        self.config, self.transport, self.token_for, self.revoke = config, transport, token_for, revoke
        self.minted: dict = {}

    def get(self, agent: str, repo: str, permissions: dict) -> str:
        key = (agent, repo.lower())
        if key not in self.minted:
            self.minted[key] = self.token_for(agent, repo, permissions, self.config, self.transport)
        return self.minted[key][0]

    def close(self) -> None:
        for token, revocable in self.minted.values():
            if revocable:
                self.revoke(token, self.transport)
        self.minted.clear()


def _caller(api, transport, tokens: _Tokens, agent: str, permissions: dict) -> Call:
    def call(method: str, path: str, body: dict | None) -> tuple[dict, object]:
        match = re.match(r"^/repos/([^/]+/[^/?]+)", path)
        if not match:
            raise IdentityError(f"refused: {path.split('?')[0]} is not a repository path")
        return api(transport, method, path, f"token {tokens.get(agent, match.group(1), permissions)}", body)

    return call


def jira_status_with_atl(atl: str, run) -> Callable[[str], str]:
    """A key's Jira status, read as Talia with atl (her own Jira user). A failure refuses the merge."""

    def status(key: str) -> str:
        result = run([atl, "talia", "jira-get", key], capture_output=True, text=True)
        try:
            return json.loads(result.stdout)["status"]
        except (ValueError, KeyError, TypeError):
            raise IdentityError(f"refused: Jira can't be read for {key} (atl talia jira-get)") from None

    return status


def cli(agent: str, args: list[str], config: dict, default_repo: str | None, deps: dict) -> int:
    """deps: api, transport, token_for, revoke, read_permissions, jira, dry_run, out (print), stdin."""
    out = deps["out"]
    tokens = _Tokens(config, deps["transport"], deps["token_for"], deps["revoke"])
    reader_perms = deps["read_permissions"](config["agents"]["reader"])
    read = _caller(deps["api"], deps["transport"], tokens, "reader", reader_perms)
    try:
        if agent == "sync":
            if deps.get("dry_run"):
                out(f"agent-gh dry run: sync {args[0]} on {DELEGATION_REPO}")
                return 0
            write = _caller(deps["api"], deps["transport"], tokens, "sync", RECORD_WRITE)
            out(sync_command(args, deps["stdin"](), read, write))
            return 0
        if agent == "reader":
            if len(args) != 2:
                raise IdentityError(USAGE)
            try:
                when = _time(args[1])
            except ValueError:
                raise IdentityError(f"'{args[1]}' is not an ISO time") from None
            found = covering(read, when)
            if not found:
                out(f"no delegation covers {when:%Y-%m-%d %H:%M} UTC")
                return 1
            out(f"{found.url} {found.start:%Y-%m-%dT%H:%M:%SZ} {found.end:%Y-%m-%dT%H:%M:%SZ} {found.opened_by}")
            return 0
        do_merge = args[:2] == ["pr", "merge"]
        repo, number = _pr_args(args[2:] if do_merge else args[1:], default_repo)
        if deps.get("dry_run"):
            out(f"agent-gh dry run: talia delegated {'merge' if do_merge else 'merge-check'} of {repo}#{number}")
            return 0
        checked = check_merge(repo, number, read, deps["jira"])
        for line in checked.passed:
            out(f"ok {line}")
        for line in checked.failures:
            out(f"FAIL {line}")
        if checked.failures:
            out(f"{repo}#{number}: not merged on a delegation ({len(checked.failures)} check(s) failed)")
            return 1
        if not do_merge:
            out(f"{repo}#{number}: every check holds; agent-gh talia pr merge {number} -R {repo} would merge it")
            return 0
        write = _caller(deps["api"], deps["transport"], tokens, "talia", MERGE_WRITE)
        answer = merge(checked, write)
        out(f"merged {repo}#{number} as {str(answer.get('sha', ''))[:7]} on {checked.delegation.url}")
        for line in trace(checked, answer, write):
            out(line)
        out(
            "next: list the merge on the delegation issue (delegation-record.sh note), "
            "comment on the Jira issue, and brief Ahmed when the window ends"
        )
        return 0
    finally:
        tokens.close()
