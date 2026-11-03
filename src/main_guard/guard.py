"""Rules for Main guard.

On GitHub Free, private repositories can't block pushes to `main`, and an agent's GitHub App with
Contents write could push or merge there through the API. Main guard is the detective control: it
runs on every push to `main` and turns red when any of the push's actors (the actor, the triggering
actor, the pusher and the sender) is not on the allowed list. The allowed list comes from the
caller's workflow file, never from a file the push itself could change: workflow files are the
one thing the agents' Apps cannot push (decision D4). A force push by an allowed actor is a warning.

`merge_only` actors (Talia's App, BASITA-162, plan 10.9) may update main only by merging a pull
request: the workflow says whether the new head is the merge commit of a merged PR (`pr_merge`). A plain
push or a force push by one of them is an error, like an unknown actor's.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Push:
    ref: str
    actors: tuple[str, ...]
    forced: bool = False
    before: str = ""
    after: str = ""


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" or "warning"
    message: str


def check(
    push: Push, allowed: frozenset[str], merge_only: frozenset[str] = frozenset(), pr_merge: bool = False
) -> list[Finding]:
    """Findings for one push. `allowed` and `merge_only` hold lower-case GitHub logins."""
    actors = sorted({a for a in push.actors if a})
    if not actors:
        return [Finding("error", f"{push.ref} was updated and the push names no actor")]
    findings = []
    mergers = [a for a in actors if a.lower() in merge_only and a.lower() not in allowed]
    unknown = [a for a in actors if a.lower() not in allowed and a.lower() not in merge_only]
    if unknown:
        findings.append(
            Finding(
                "error",
                f"{push.ref} was updated by {', '.join(unknown)} ({push.before[:7]}..{push.after[:7]}), "
                f"who is not allowed to update it (allowed: {', '.join(sorted(allowed)) or 'nobody'})",
            )
        )
    if mergers and (not pr_merge or push.forced):
        how = "force-pushed" if push.forced else "pushed, not as the merge of a pull request"
        findings.append(
            Finding(
                "error",
                f"{push.ref} was {how} by {', '.join(mergers)} ({push.before[:7]}..{push.after[:7]}), "
                "who may update it only by merging a pull request",
            )
        )
    if push.forced:
        findings.append(Finding("warning", f"{push.ref} was force-pushed by {', '.join(actors)}"))
    return findings
