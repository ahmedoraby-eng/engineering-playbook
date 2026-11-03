"""Whether the reviewer's review covers the PR's head commit.

Green when the PR needs no review, or the reviewer's latest review is an approval of the head
commit. An approval of an older commit still counts when the change set is identical (the same
files with the same blob SHAs against the base branch): that is a merge from the base branch that
touched none of the PR's files, and a re-review would find nothing new. Any real change, or any
doubt (a file list that could not be read, a commit no longer reachable), needs a re-review of the
delta since the approved commit, at the same effort. Comment-only reviews change nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .rules import Classification

APPROVED, CHANGES, DISMISSED = "APPROVED", "CHANGES_REQUESTED", "DISMISSED"
# Waiting states; `pickup` (T4) starts the reviewer on `waiting` and the author on `changes`.
GREEN, WAITING, CHANGES_STATE = "green", "waiting", "changes"


@dataclass(frozen=True)
class Review:
    login: str
    state: str
    commit_id: str

    @classmethod
    def from_github(cls, data: dict) -> Review:
        return cls(
            login=(data.get("user") or {}).get("login") or "",
            state=data.get("state") or "",
            commit_id=data.get("commit_id") or "",
        )


@dataclass(frozen=True)
class Decision:
    state: str  # green, waiting or changes
    status: str  # one line for people
    reviewed_sha: str = ""  # the reviewer's last approved or reviewed commit, for a delta review
    request: bool = False  # ask the reviewer (again)

    @property
    def green(self) -> bool:
        return self.state == GREEN


def latest_review(reviews: list[Review], reviewer: str) -> Review | None:
    """The reviewer's latest review that set a state, in the API's chronological order."""
    own = [r for r in reviews if r.login.lower() == reviewer.lower() and r.state in (APPROVED, CHANGES, DISMISSED)]
    return own[-1] if own else None


def decide(
    classification: Classification,
    reviewer: str,
    head_sha: str,
    reviews: list[Review],
    same_changes: Callable[[str], bool],
) -> Decision:
    """Decide the check. `same_changes(sha)` says whether `sha` has the head's exact change set."""
    if not classification.required:
        return Decision(GREEN, f"no review needed ({classification.cls}: {'; '.join(classification.reasons)})")
    tier = classification.effort
    last = latest_review(reviews, reviewer)
    if last is None:
        return Decision(WAITING, f"waiting for {reviewer} (tier {tier})", request=True)
    short = last.commit_id[:7]
    if last.state == APPROVED and last.commit_id == head_sha:
        return Decision(GREEN, f"approved by {reviewer} on the head commit {short}", last.commit_id)
    if last.state == APPROVED:
        if same_changes(last.commit_id):
            return Decision(GREEN, f"approval carried from {short}: same changes", last.commit_id)
        return Decision(
            WAITING,
            f"{reviewer} approved {short}, which does not cover the new changes: "
            f"review the delta {short}..{head_sha[:7]} (tier {tier})",
            last.commit_id,
            request=True,
        )
    if last.state == CHANGES and last.commit_id == head_sha:
        return Decision(CHANGES_STATE, f"{reviewer} requested changes on {short}", last.commit_id)
    if last.state == CHANGES:
        return Decision(
            WAITING,
            f"{reviewer} requested changes on {short}; the new commits wait for review "
            f"of the delta {short}..{head_sha[:7]} (tier {tier})",
            last.commit_id,
            request=True,
        )
    return Decision(WAITING, f"{reviewer}'s review was dismissed: waiting for a new review (tier {tier})", request=True)


def change_set(files: list[dict] | None) -> frozenset[tuple[str, str, str, str]] | None:
    """The files of a `compare` response as comparable tuples, or None when unknown or cut short."""
    # The compare API lists at most 300 files; a full page means there may be more.
    if files is None or len(files) >= 300:
        return None
    return frozenset(
        (f.get("filename") or "", f.get("previous_filename") or "", f.get("status") or "", f.get("sha") or "")
        for f in files
    )
