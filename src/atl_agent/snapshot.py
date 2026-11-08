"""The jira-md snapshot's content rules (BASITA-219, Yousef's design work/yousef/BASITA-219-snapshot-design.md).

Two files per issue. The core file (`<KEY>.md`) is what an agent reads: the fields, the description, the links, the
sub-tasks, a current-state block, the comments the rules below pin, the last three comments and an index line for every
other comment. The archive (`<KEY>.full.md`) holds every comment verbatim and the changelog, and is read by line range.

Nothing is summarised or rewritten: a comment is shown whole, or it is one index line, and the archive has it whole.
The selection is deterministic (same comments, same file), runs inside the fetch jira-md already makes, and calls no
model. Any doubt pins. This module is pure (no Jira calls, no file access) so the rules are testable on fixtures.

A comment record is a dict: id, created, updated, author (display name), account (account id), text (Markdown).
"""

from __future__ import annotations

import datetime
import re

LAST_COMMENTS = 3  # the freshest comments are always shown, pinned or not
CURRENT_STATE_CAP = 1200  # characters of a latest-comment-per-author shown in the current-state block
INDEX_TEXT = 80  # characters of a comment's first line in the index
CORE_TOKEN_CAP = 8000  # the core file's size cap (Yousef's design: target 6k tokens, hard cap 8k)
BYTES_PER_TOKEN = 3  # a deliberately low estimate (English is ~4, Arabic ~3-4 bytes per token), so the cap is safe
EDITED_AFTER = 2  # seconds between created and updated before a comment counts as edited
HISTORY_VALUE = 80  # a changelog value is cut to this many characters
HISTORY_TEXT_FIELDS = ("description", "environment", "comment")  # long text: the line says "changed", not the text

TAGS = ("decision:", "handoff:", "state:", "blocked:", "ac change:")
KEYWORDS = (
    "decision",
    "decided",
    "approved",
    "rejected",
    "blocked",
    "dropped",
    "waiting for",
    "waits for",
    "replaces",
    "supersedes",
    "changes ac",
    "قرار",
    "موافق",
    "رفض",
    "متوقف",
    "بدل",
    "يلغي",
)
# «a quote» next to Ahmed's name: within 60 characters, no other quote between them
_QUOTE_NEXT_TO_AHMED = re.compile(r"(?:ahmed|أحمد)[^«»]{0,60}«[^»]+»|«[^»]+»[^«»]{0,60}(?:ahmed|أحمد)", re.IGNORECASE)
# Any other mention of Ahmed ("Ahmed dropped AC5"): the recall check on BASITA-219's sample found a scope decision of
# his that a list of verbs missed (DFP-169 #11208), so any mention pins. Any doubt pins.
_AHMED_MENTION = re.compile(r"\bahmed\b|أحمد", re.IGNORECASE)
_SIGNED_BY_AGENT = re.compile(r"—\s*\w+\s*\(Basita AI[^)]*\)\s*$")
_REPLACES = re.compile(
    r"(?:replaces|supersedes|يلغي)[^.\n]{0,60}?(?:comment\s+|#|focusedCommentId=)(\d{3,})", re.IGNORECASE
)


def parse_time(value: str | None) -> datetime.datetime | None:
    """A Jira timestamp (2026-11-08T23:49:54.817+0200) as a datetime, or None."""
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")  # noqa: DTZ007 - the offset is in the text
    except ValueError:
        return None


def one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def is_signed_by_agent(text: str) -> bool:
    return bool(_SIGNED_BY_AGENT.search(text.strip()))


def pin_reasons(comment: dict, later: list[dict], people: dict) -> list[str]:
    """Why a comment is pinned: the rules of the design's section 3 that hold for it. Empty when none does.
    `later` are the comments after it; `people` holds the account ids of "ahmed" and "adam" (sets)."""
    text = comment.get("text") or ""
    lowered = text.lower()
    reasons = []
    if comment.get("account") in people.get("ahmed", ()) and not is_signed_by_agent(text):
        reasons.append("author is Ahmed")  # an agent's signed comment posted from his account is not his
    if _QUOTE_NEXT_TO_AHMED.search(text):
        reasons.append("quotes Ahmed")
    elif _AHMED_MENTION.search(text):
        reasons.append("mentions Ahmed")
    first = lowered.lstrip().split("\n", 1)[0]
    tag = next((t for t in TAGS if first.startswith(t)), None)
    if tag:
        reasons.append(f"tag {tag.rstrip(':')}")
    word = next((k for k in KEYWORDS if k in lowered), None)
    if word:
        reasons.append(f"keyword {word}")
    if comment.get("account") in people.get("adam", ()):
        reasons.append("author is Adam")
    number = str(comment.get("id") or "")
    if number and any(
        re.search(rf"(?<!\d){re.escape(number)}(?!\d)", later_one.get("text") or "") for later_one in later
    ):
        reasons.append("cited by a later comment")
    created, updated = parse_time(comment.get("created")), parse_time(comment.get("updated"))
    if created and updated and (updated - created).total_seconds() > EDITED_AFTER:
        reasons.append("edited after posting")
    return reasons


def superseded_by(comments: list[dict], people: dict) -> dict[str, str]:
    """{comment id: the id of the later comment that says it replaces or supersedes it}. Ahmed's own comments are
    never superseded (any doubt pins)."""
    found: dict[str, str] = {}
    position = {c["id"]: i for i, c in enumerate(comments)}
    for index, comment in enumerate(comments):
        for target in _REPLACES.findall(comment.get("text") or ""):
            if target in position and position[target] < index:
                owner = comments[position[target]]
                if owner.get("account") in people.get("ahmed", ()) and not is_signed_by_agent(owner.get("text") or ""):
                    continue
                found.setdefault(target, comment["id"])
    return found


def select_comments(comments: list[dict], people: dict, link_marker: str = "") -> dict:
    """Which comments the core file shows and why. Returns {"pinned": {id: [reasons]}, "last": [ids],
    "superseded": {id: id}, "link": id or None}. A comment holding `link_marker` is the snapshot's own link comment:
    never pinned, never one of the last three, still in the index."""
    ordered = sorted(comments, key=lambda c: (c.get("created") or "", str(c.get("id"))))
    link = next((c["id"] for c in ordered if link_marker and link_marker in (c.get("text") or "")), None)
    superseded = superseded_by(ordered, people)
    pinned = {}
    for index, comment in enumerate(ordered):
        if comment["id"] == link or comment["id"] in superseded:
            continue
        reasons = pin_reasons(comment, ordered[index + 1 :], people)
        if reasons:
            pinned[comment["id"]] = reasons
    last = [c["id"] for c in ordered if c["id"] != link][-LAST_COMMENTS:]
    return {"pinned": pinned, "last": last, "superseded": superseded, "link": link}


def comment_heading(comment: dict) -> str:
    return f"### #{comment['id']} · {comment.get('created')} · {comment.get('author') or 'Unknown'}"


def history_entries(histories: list[dict]) -> list[dict]:
    """One entry per changed field, oldest first: {"when", "who", "field", "from", "to"}. Long text fields (the
    description, a comment) say "changed" instead of carrying both texts; other values are cut to HISTORY_VALUE."""
    entries = []
    for history in sorted(histories, key=lambda h: h.get("created") or ""):
        who = (history.get("author") or {}).get("displayName") or "Unknown"
        for change in history.get("items", []):
            field = change.get("field") or "?"
            if field.lower() in HISTORY_TEXT_FIELDS:
                before, after = None, "changed"
            else:
                before, after = (one_line(change.get(k)) or None for k in ("fromString", "toString"))
                before, after = (v[:HISTORY_VALUE] if v else v for v in (before, after))
            entries.append({"when": history.get("created"), "who": who, "field": field, "from": before, "to": after})
    return entries


def history_line(entry: dict) -> str:
    before = entry["from"] if entry["from"] else "(empty)"
    after = entry["to"] if entry["to"] else "(empty)"
    change = after if entry["to"] == "changed" and entry["from"] is None else f"{before} → {after}"
    return f"- {entry['when']} · {entry['who']} · {entry['field']}: {change}"


def render_archive(issue: str, header: str, comments: list[dict], entries: list[dict]) -> tuple[str, dict]:
    """The archive text and {comment id: (first line, last line)} of each comment in it (1-based, for `sed -n`)."""
    lines = [f"# {issue}: archive (every comment and the changelog)", "", header]
    ranges: dict[str, tuple[int, int]] = {}
    lines += ["", f"## Comments ({len(comments)}, oldest first)"]
    for comment in comments:
        lines += ["", comment_heading(comment)]
        start = len(lines)  # every entry of `lines` is one line, so its length is the heading's line number
        if comment.get("text"):
            lines += ["", *comment["text"].split("\n")]
        ranges[comment["id"]] = (start, len(lines))
    if entries:
        lines += ["", f"## History ({len(entries)} changes, oldest first: when · who · field: from → to)", ""]
        lines += [history_line(entry) for entry in entries]
    return "\n".join(lines) + "\n", ranges


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip()


def _index_line(comment: dict, ranges: dict, flags: list[str]) -> str:
    first = next((one_line(line) for line in (comment.get("text") or "").split("\n") if line.strip()), "")
    cut = _short(first, INDEX_TEXT) + ("…" if len(first) > INDEX_TEXT else "")
    start, end = ranges[comment["id"]]
    tail = f" [{'; '.join(flags)}]" if flags else ""
    return (
        f"- #{comment['id']} · {comment.get('created')} · {comment.get('author') or 'Unknown'} · {cut}{tail} "
        f"(archive lines {start}-{end})"
    )


def estimate_tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def render_core(
    top: list[str],
    comments: list[dict],
    chosen: dict,
    ranges: dict,
    issue: str,
    history: list[dict],
    cap: int = CORE_TOKEN_CAP,
) -> tuple[str, dict]:
    """The core file: `top` (title to attachments, already rendered), then Current state, the shown comments, the index
    and the archive pointer. The oldest pinned comments go to the index, flagged, while the file is over `cap` tokens.
    Returns the text and {"shown": [ids], "indexed": [ids], "over_cap": [ids]}."""
    ordered = sorted(comments, key=lambda c: (c.get("created") or "", str(c.get("id"))))
    by_id = {c["id"]: c for c in ordered}
    last = set(chosen["last"])
    over_cap: list[str] = []

    def build() -> tuple[str, list[str], list[str]]:
        shown = [
            c["id"] for c in ordered if (c["id"] in chosen["pinned"] or c["id"] in last) and c["id"] not in over_cap
        ]
        indexed = [c["id"] for c in ordered if c["id"] not in shown]
        lines = list(top)
        lines += _current_state(ordered, shown, history, chosen.get("link"))
        total = len(ordered)
        if total:
            lines += [
                "",
                f"## Comments ({len(shown)} of {total} shown: pinned by rule, and the last {LAST_COMMENTS}; "
                f"{len(indexed)} more are in the index below)",
            ]
            for number in shown:
                comment = by_id[number]
                reasons = chosen["pinned"].get(number, [])
                tag = f" [pinned: {', '.join(reasons)}]" if reasons else ""
                lines += ["", comment_heading(comment) + tag]
                if comment.get("text"):
                    lines += ["", comment["text"]]
        if indexed:
            lines += ["", f"## Other comments, index ({len(indexed)}; the text is in the archive)", ""]
            for number in indexed:
                flags = []
                if number in over_cap:
                    flags.append("pinned, over cap")
                if number in chosen["superseded"]:
                    flags.append(f"superseded by #{chosen['superseded'][number]}")
                lines.append(_index_line(by_id[number], ranges, flags))
        lines += ["", "## Archive", "", _archive_pointer(issue, total, len(history))]
        return "\n".join(lines) + "\n", shown, indexed

    text, shown, indexed = build()
    # Oldest first, but Ahmed's own comments and quotes of him leave last: they are the ones nobody may lose.
    candidates = [c["id"] for c in ordered if c["id"] in chosen["pinned"] and c["id"] not in last]
    ahmeds = [n for n in candidates if {"author is Ahmed", "quotes Ahmed"} & set(chosen["pinned"][n])]
    pinned_old_first = [n for n in candidates if n not in ahmeds] + ahmeds
    while estimate_tokens(text) > cap and pinned_old_first:
        over_cap.append(pinned_old_first.pop(0))
        text, shown, indexed = build()
    return text, {"shown": shown, "indexed": indexed, "over_cap": over_cap}


def _archive_pointer(issue: str, total: int, changes: int) -> str:
    return (
        f"work/jira/{issue}.full.md holds all {total} comments verbatim and the changelog ({changes} changes). To read "
        f"an archived comment, `sed -n 'START,ENDp' work/jira/{issue}.full.md` with the lines in its index entry "
        f"(or `grep -n '^### #<id>' work/jira/{issue}.full.md`); do not open the whole file. Live, as yourself: "
        f"`atl <agent> jira-get {issue} --json --comments all --history`, `atl <agent> jira-get {issue} --history`."
    )


def _current_state(ordered: list[dict], shown: list[str], history: list[dict], link: str | None) -> list[str]:
    """What a reader needs first, all from data: the last status change, the last comment, the latest comment of each
    author (verbatim, capped, or where it is shown below). Status, assignee, versions and labels are the field list."""
    lines = ["", "## Current state", ""]
    moves = [h for h in history if h["field"].lower() == "status"]
    if moves:
        move = moves[-1]
        change = f"{move['from'] or '(empty)'} → {move['to'] or '(empty)'}"
        lines.append(f"- **Last status change:** {change}, {move['who']}, {move['when']}")
    newest = [c for c in ordered if c["id"] != link][-1:]
    if newest:
        lines.append(f"- **Last comment:** #{newest[0]['id']}, {newest[0].get('author')}, {newest[0].get('created')}")
    latest: dict[str, dict] = {}
    for comment in ordered:
        if comment["id"] == link:
            continue
        latest[comment.get("author") or "Unknown"] = comment
    for author, comment in sorted(latest.items(), key=lambda item: item[1].get("created") or ""):
        text = comment.get("text") or ""
        if comment["id"] in shown:
            lines.append(f"- **Latest by {author}:** #{comment['id']}, shown below")
        elif len(text) <= CURRENT_STATE_CAP:
            quoted = "\n  > ".join(text.split("\n"))
            lines.append(f"- **Latest by {author}:** #{comment['id']}, {comment.get('created')}\n  > {quoted}")
        else:
            quoted = "\n  > ".join(text[:CURRENT_STATE_CAP].rstrip().split("\n"))
            lines.append(
                f"- **Latest by {author}:** #{comment['id']}, {comment.get('created')}\n  > {quoted}\n"
                f"  > …full text: archive #{comment['id']}"
            )
    return lines
