"""The jira-md snapshot's content rules (BASITA-219, Yousef's design work/yousef/BASITA-219-snapshot-design.md).

Two files per issue. The core file (`<KEY>.md`) is what an agent reads: the fields, the description, the links, the
sub-tasks, a current-state block, the comments the rules below pin, the last three comments and an index line for every
other comment. The archive (`<KEY>.full.md`) holds every comment verbatim and the changelog, and is read by line range.

Two strengths of pin (Yousef's review of PR #52, 09/11/2026). A strong pin (Ahmed's own comment, a «quote» of him, a
first-line tag, a comment a later one cites, a comment that replaces an earlier one) is shown whole. A weak pin (a
keyword, a plain mention of Ahmed, Adam's comment, an edited one) is shown as its first WEAK_CHARS characters and the
archive lines of the rest. The last three comments are shown whole whatever they are. Generic keywords (decision,
approved, rejected, blocked) count only at the start of a line, because almost every routine agent report holds them.

Nothing is summarised or rewritten: a comment is shown whole, cut at a stated point with the archive lines of the rest,
or it is one index line, and the archive has it whole. The selection is deterministic (same comments, same file), runs
inside the fetch jira-md already makes, and calls no model. Any doubt pins (at least weakly). This module is pure (no
Jira calls, no file access) so the rules are testable on fixtures.

A comment record is a dict: id, created, updated, author (display name), account (account id), text (Markdown).
"""

from __future__ import annotations

import datetime
import re

LAST_COMMENTS = 3  # the freshest comments are always shown, pinned or not
LEGACY_COMMENTS = 10  # the file before BASITA-219 held the last 10 comments: its size is the core's budget
WEAK_CHARS = 600  # characters of a weakly pinned comment shown in the core; the rest is in the archive
INDEX_TEXT = 80  # characters of a comment's first line in the index
CORE_TOKEN_CAP = 8000  # the core file's size cap (Yousef's design: target 6k tokens, hard cap 8k)
BYTES_PER_TOKEN = 3  # a deliberately low estimate (English is ~4, Arabic ~3-4 bytes per token), so the cap is safe
EDITED_AFTER = 2  # seconds between created and updated before a comment counts as edited
HISTORY_VALUE = 80  # a changelog value is cut to this many characters
HISTORY_TEXT_FIELDS = ("description", "environment", "comment")  # long text: the line says "changed", not the text

TAGS = ("decision:", "handoff:", "state:", "blocked:", "ac change:")
# Keywords that pin (weakly) wherever they are: they name a change of scope, a wait or a replacement.
KEYWORDS = (
    "decided",
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
    "يلغي",
)
# Generic words that routine reports are full of ("approved by Yousef", "not blocked"): they pin (weakly) only at the
# start of a line, after any list or emphasis marker ("Decision (Ahmed): ...", "**Approved** ...", "- Blocked on ...").
LINE_KEYWORDS = ("decision", "approved", "rejected", "blocked")
_LINE_KEYWORD = re.compile(r"^[\s>*_#\-+\d.)]*(" + "|".join(LINE_KEYWORDS) + r")\b", re.IGNORECASE | re.MULTILINE)
STRONG = ("author is Ahmed", "quotes Ahmed", "tag ", "cited by a later comment", "replaces #")
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
    line_word = _LINE_KEYWORD.search(text)
    if word:
        reasons.append(f"keyword {word}")
    elif line_word:
        reasons.append(f"keyword {line_word.group(1).lower()}")
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


def is_strong(reasons: list[str]) -> bool:
    """A strong pin is shown whole; a weak one (keywords, a plain mention of Ahmed, Adam, edited) is cut."""
    return any(reason.startswith(STRONG) for reason in reasons)


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
    "superseded": {id: id}, "link": id or None}. A comment that replaces an earlier one gets the strong reason
    "replaces #<id>". A comment holding `link_marker` is the snapshot's own link comment:
    never pinned, never one of the last three, still in the index."""
    ordered = sorted(comments, key=lambda c: (c.get("created") or "", str(c.get("id"))))
    link = next((c["id"] for c in ordered if link_marker and link_marker in (c.get("text") or "")), None)
    superseded = superseded_by(ordered, people)
    replaces: dict[str, list[str]] = {}
    for old, new in superseded.items():
        replaces.setdefault(new, []).append(f"replaces #{old}")
    pinned = {}
    for index, comment in enumerate(ordered):
        if comment["id"] == link or comment["id"] in superseded:
            continue
        reasons = pin_reasons(comment, ordered[index + 1 :], people) + replaces.get(comment["id"], [])
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


def cut_text(text: str, limit: int | None = None) -> str | None:
    """A weakly pinned comment's first `limit` characters, cut at the last line break or space before it, with any
    open code fence closed; None when the comment is short enough to show whole. Nothing is rewritten."""
    limit = WEAK_CHARS if limit is None else limit
    if len(text) <= limit:
        return None
    head = text[:limit]
    stop = max(head.rfind("\n"), head.rfind(" "))
    head = (head[:stop] if stop > limit // 2 else head).rstrip()
    if sum(1 for line in head.split("\n") if line.lstrip().startswith("```")) % 2:
        head += "\n```"
    return head


def _index_line(comment: dict, ranges: dict, flags: list[str], link: bool = False) -> str:
    start, end = ranges[comment["id"]]
    if link:
        return f"- #{comment['id']} · the snapshot's link comment (lines {start}-{end})"
    first = next((one_line(line) for line in (comment.get("text") or "").split("\n") if line.strip()), "")
    cut = _short(first, INDEX_TEXT) + ("…" if len(first) > INDEX_TEXT else "")
    tail = f" [{'; '.join(flags)}]" if flags else ""
    author = comment.get("author") or "Unknown"
    return f"- #{comment['id']} · {_day(comment)} · {author} · {cut}{tail} (lines {start}-{end})"


def _day(comment: dict) -> str:
    """The comment's date and minute ("2026-11-09 01:14"), enough to place it; the heading in the archive has it all."""
    created = comment.get("created") or ""
    return f"{created[:10]} {created[11:16]}".strip()


def estimate_tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def legacy_size(top: list[str], comments: list[dict]) -> int:
    """Bytes of the file jira-md wrote before BASITA-219 (the top, then the last LEGACY_COMMENTS comments whole) for
    the same issue: the core's size budget, so a ticket read never costs more than it did (Ahmed: don't raise
    consumption; Yousef's review of PR #52, point 4)."""
    lines = list(top)
    if comments:
        count = len(comments)
        lines += ["", f"## Comments ({f'last {LEGACY_COMMENTS} of {count}' if count > LEGACY_COMMENTS else count})"]
        for comment in comments[-LEGACY_COMMENTS:]:
            lines += ["", f"### {comment.get('author') or 'Unknown'} · {comment.get('created')}"]
            if comment.get("text"):
                lines += ["", comment["text"]]
    return len(("\n".join(lines) + "\n").encode("utf-8"))


def render_core(
    top: list[str],
    comments: list[dict],
    chosen: dict,
    ranges: dict,
    issue: str,
    history: list[dict],
    cap: int = CORE_TOKEN_CAP,
    budget: int | None = None,
) -> tuple[str, dict]:
    """The core file: `top` (title to attachments, already rendered), then Current state, the shown comments, the index
    and the archive pointer. A strong pin and the last three are shown whole, a weak pin is cut at WEAK_CHARS with the
    archive lines of the rest.

    Size: the file stays within `budget` bytes (default: legacy_size, what the old "last 10" file took) and `cap`
    tokens. While it is over, one step at a time, oldest first: weak pins go to the index; the two comments before
    the newest are cut, unless strongly pinned; then strong pins are cut, then go to the index; Ahmed's own comments
    and quotes of him are cut and leave last. Then each step is undone again, most important first, when the file
    still fits. A pin in the index says "pinned, over cap", so the loss is visible and one sed away. The last three
    never go to the index and the newest comment is never cut.
    Returns the text and {"shown", "indexed", "over_cap", "strong", "weak", "cut", "budget"} (lists of ids)."""
    ordered = sorted(comments, key=lambda c: (c.get("created") or "", str(c.get("id"))))
    by_id = {c["id"]: c for c in ordered}
    last = set(chosen["last"])
    strong = [c["id"] for c in ordered if c["id"] in chosen["pinned"] and is_strong(chosen["pinned"][c["id"]])]
    weak = [c["id"] for c in ordered if c["id"] in chosen["pinned"] and c["id"] not in strong]
    budget = legacy_size(top, ordered) if budget is None else budget
    over_cap: list[str] = []
    cut_more: list[str] = []  # strong pins cut to fit the budget

    def build() -> tuple[str, list[str], list[str], list[str]]:
        shown = [
            c["id"] for c in ordered if (c["id"] in chosen["pinned"] or c["id"] in last) and c["id"] not in over_cap
        ]
        indexed = [c["id"] for c in ordered if c["id"] not in shown]
        cut: list[str] = []
        lines = list(top)
        lines += _current_state(ordered, shown, indexed, history, chosen.get("link"))
        total = len(ordered)
        if shown:
            lines += ["", f"## Comments ({len(shown)} of {total}: pinned by rule, and the last {LAST_COMMENTS})"]
            for number in shown:
                comment = by_id[number]
                reasons = chosen["pinned"].get(number, [])
                tag = f" [pinned: {', '.join(reasons)}]" if reasons else ""
                lines += ["", comment_heading(comment) + tag]
                text = comment.get("text") or ""
                cutting = number in cut_more or (number in weak and number not in last)
                short = cut_text(text) if cutting else None
                if short is not None:
                    start, end = ranges[number]
                    lines += ["", short, f"…rest: archive lines {start}-{end}"]
                    cut.append(number)
                elif text:
                    lines += ["", text]
        if indexed:
            lines += ["", f"## Other comments ({len(indexed)}; one line each, the text is in the archive)", ""]
            for number in indexed:
                link = number == chosen.get("link")
                lines.append(_index_line(by_id[number], ranges, _flags(number, chosen, over_cap), link))
        lines += ["", "## Archive", "", _archive_pointer(issue, total, len(history))]
        return "\n".join(lines) + "\n", shown, indexed, cut

    def over(text: str) -> bool:
        return estimate_tokens(text) > cap or len(text.encode("utf-8")) > budget

    movable = [n for n in chosen["pinned"] if n not in last]
    ahmeds = [n for n in movable if {"author is Ahmed", "quotes Ahmed"} & set(chosen["pinned"][n])]
    others = [n for n in strong if n in movable and n not in ahmeds]
    newest = chosen["last"][-1:]
    fresh = [n for n in chosen["last"] if n not in strong and n not in newest]  # the two before the newest
    steps = (
        [("index", n) for n in weak if n in movable and n not in ahmeds]
        + [("cut", n) for n in fresh]
        + [("cut", n) for n in others]
        + [("index", n) for n in others]
        + [("cut", n) for n in ahmeds]
        + [("index", n) for n in ahmeds]
    )
    text, shown, indexed, cut = build()
    applied = []
    for kind, number in steps:
        if not over(text):
            break
        (over_cap if kind == "index" else cut_more).append(number)
        applied.append((kind, number))
        text, shown, indexed, cut = build()
    # A big comment cut can free more room than was needed: give back what still fits, most important first.
    for kind, number in reversed(applied):
        target = over_cap if kind == "index" else cut_more
        target.remove(number)
        candidate = build()
        if over(candidate[0]):
            target.append(number)
        else:
            text, shown, indexed, cut = candidate
    if applied:
        text, shown, indexed, cut = build()
    stats = {
        "shown": shown,
        "indexed": indexed,
        "over_cap": over_cap,
        "strong": strong,
        "weak": weak,
        "cut": cut,
        "budget": budget,
    }
    return text, stats


def _flags(number: str, chosen: dict, over_cap: list[str]) -> list[str]:
    flags = []
    if number in over_cap:
        flags.append("pinned, over cap")
    if number in chosen["superseded"]:
        flags.append(f"superseded by #{chosen['superseded'][number]}")
    return flags


def _archive_pointer(issue: str, total: int, changes: int) -> str:
    path = f"work/jira/{issue}.full.md"
    return (
        f"{path}: all {total} comments verbatim and the changelog ({changes} changes). Read a comment by its lines, "
        f"`sed -n 'A,Bp' {path}`, never the whole file. Live: `atl <agent> jira-get {issue} --json --comments all "
        f"--history`."
    )


def _current_state(
    ordered: list[dict], shown: list[str], indexed: list[str], history: list[dict], link: str | None
) -> list[str]:
    """What a reader needs first, all from data: the last status change and where each author's latest comment is
    (newest last, so the last one is the last comment). Pointers only: the text is below or in the archive (Yousef's
    review, point 3). Status, assignee, versions and labels are the field list."""
    lines = ["", "## Current state", ""]
    moves = [h for h in history if h["field"].lower() == "status"]
    if moves:
        move = moves[-1]
        change = f"{move['from'] or '(empty)'} → {move['to'] or '(empty)'}"
        lines.append(f"- **Last status change:** {change}, {move['who']}, {move['when']}")
    latest: dict[str, dict] = {}
    for comment in ordered:
        if comment["id"] != link:
            latest[comment.get("author") or "Unknown"] = comment
    if latest and [n for n in indexed if n != link]:  # when every comment is below, the comments say it
        where = ", ".join(
            f"{author} #{c['id']} {_day(c)} ({'below' if c['id'] in shown else 'index'})"
            for author, c in sorted(latest.items(), key=lambda item: item[1].get("created") or "")
        )
        lines.append(f"- **Latest comment by each author (newest last):** {where}")
    if len(lines) == 3:
        return []
    return lines
