"""BASITA-205 (ADR-B001): the ADF -> Markdown renderer, `jira_md` snapshots and the jira-md / jira-get CLI."""

import json
import re
import subprocess

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main
from atl_agent.client import _adf_md, _adf_text, snapshot_marker

TOKEN = "tok-" + "y" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"
UPDATED = "2026-11-07T10:00:00.000+0200"
APP_HTTPS = "https://github.com/basitasolutions/elbayoumy-farm-nutrition-platform.git"
APP_SSH = "git@github.com:basitasolutions/elbayoumy-farm-nutrition-platform.git"
COMMENT_URL = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3/issue/BASITA-7/comment"


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        return self.responses.pop(0)


def client(transport, agent="malek"):
    return AtlClient(agent, CLOUD, TOKEN, transport=transport)


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def para(*content):
    return {"type": "paragraph", "content": list(content)}


def doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def item(*content):
    return {"type": "listItem", "content": list(content)}


def cell(value, kind="tableCell"):
    return {"type": kind, "content": [para(text(value))]}


# --- the renderer -------------------------------------------------------------------------------------------------


def test_headings_take_their_level_and_the_offset():
    heading = {"type": "heading", "attrs": {"level": 2}, "content": [text("Acceptance criteria")]}
    assert _adf_md(doc(heading)) == "## Acceptance criteria"
    assert _adf_md(doc(heading), heading_offset=2) == "#### Acceptance criteria"
    assert _adf_md(doc({**heading, "attrs": {"level": 6}}), heading_offset=3) == "###### Acceptance criteria"


def test_nested_lists_are_indented_by_their_marker():
    nested = {"type": "bulletList", "content": [item(para(text("inner")))]}
    ordered = {
        "type": "orderedList",
        "attrs": {"order": 3},
        "content": [item(para(text("first")), nested), item(para(text("second")))],
    }
    bullets = {"type": "bulletList", "content": [item(para(text("top")), ordered)]}
    assert _adf_md(doc(bullets)) == "- top\n  3. first\n     - inner\n  4. second"


def test_a_table_is_a_pipe_table_with_the_first_row_as_header_and_pipes_escaped():
    table = {
        "type": "table",
        "content": [
            {"type": "tableRow", "content": [cell("Rule", "tableHeader"), cell("Value", "tableHeader")]},
            {"type": "tableRow", "content": [cell("a|b"), {"type": "tableCell", "content": [para(text("x"))]}]},
            {"type": "tableRow", "content": [cell("short")]},
        ],
    }
    assert _adf_md(doc(table)) == "| Rule | Value |\n| --- | --- |\n| a\\|b | x |\n| short |  |"


def test_a_code_block_is_fenced_with_its_language():
    code = {"type": "codeBlock", "attrs": {"language": "python"}, "content": [text("x = 1\nprint(x)")]}
    assert _adf_md(doc(code)) == "```python\nx = 1\nprint(x)\n```"
    assert _adf_md(doc({"type": "codeBlock", "content": [text("a ``` b")]})) == "````\na ``` b\n````"


def test_marks_and_links():
    line = para(
        text("bold", {"type": "strong"}),
        text(" "),
        text("it ", {"type": "em"}),
        text("code", {"type": "code"}),
        text(" "),
        text("gone", {"type": "strike"}),
        text(" see "),
        text("docs", {"type": "link", "attrs": {"href": "https://x.test/a"}}),
        text(" "),
        text("https://y.test", {"type": "link", "attrs": {"href": "https://y.test"}}),
    )
    assert _adf_md(doc(line)) == "**bold** _it_ `code` ~~gone~~ see [docs](https://x.test/a) https://y.test"


def test_mention_emoji_status_cards_and_hard_break():
    line = para(
        {"type": "mention", "attrs": {"id": "712020:x", "text": "@Malek"}},
        text(" ok "),
        {"type": "emoji", "attrs": {"shortName": ":tick:", "text": "✅"}},
        {"type": "hardBreak"},
        {"type": "status", "attrs": {"text": "IN REVIEW", "color": "blue"}},
        text(" "),
        {"type": "inlineCard", "attrs": {"url": "https://x.test/c"}},
    )
    card = {"type": "blockCard", "attrs": {"url": "https://x.test/b"}}
    assert _adf_md(doc(line, card)) == "@Malek ok :tick:\nIN REVIEW https://x.test/c\n\nhttps://x.test/b"


def test_blockquote_panel_and_rule():
    quote = {"type": "blockquote", "content": [para(text("one")), para(text("two"))]}
    panel = {"type": "panel", "attrs": {"panelType": "info"}, "content": [para(text("note"))]}
    assert _adf_md(doc(quote, {"type": "rule"}, panel)) == "> one\n>\n> two\n\n---\n\n> note"


def test_an_unknown_node_keeps_its_childrens_text():
    unknown = {"type": "layoutSection", "content": [{"type": "layoutColumn", "content": [para(text("kept"))]}]}
    inline_unknown = para(text("a "), {"type": "futureInline", "content": [text("inside")]})
    assert _adf_md(doc(unknown, inline_unknown)) == "kept\n\na inside"
    assert _adf_md(doc({"type": "mystery", "attrs": {"url": "https://x.test/m"}})) == "https://x.test/m"
    assert _adf_md(None) == ""


def test_adf_text_is_unchanged():
    link = text("see", {"type": "link", "attrs": {"href": "https://x.test"}})
    assert _adf_text(doc(para(text("Given")), {"type": "bulletList", "content": [item(para(link))]})) == (
        "Given\n- see (https://x.test)"
    )


# --- jira_md ------------------------------------------------------------------------------------------------------


def _fields(updated=UPDATED, description=None):
    return {
        "summary": "Snapshot the issue",
        "updated": updated,
        "status": {"name": "In Progress"},
        "issuetype": {"name": "Task"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Malek (Basita AI)"},
        "reporter": {"displayName": "Ahmed Oraby"},
        "labels": ["agent-picked"],
        "fixVersions": [{"name": "0.4.0"}],
        "parent": {"key": "BASITA-1", "fields": {"summary": "Agents read files"}},
        "description": description
        or doc(
            {"type": "heading", "attrs": {"level": 1}, "content": [text("Acceptance criteria")]},
            {"type": "bulletList", "content": [item(para(text("AC1 works")))]},
        ),
        "issuelinks": [
            {
                "type": {"outward": "blocks", "inward": "is blocked by"},
                "outwardIssue": {"key": "DFP-1", "fields": {"summary": "A", "status": {"name": "Done"}}},
            },
            {
                "type": {"outward": "blocks", "inward": "is blocked by"},
                "inwardIssue": {"key": "DFP-2", "fields": {"summary": "B", "status": {"name": "To Do"}}},
            },
        ],
        "subtasks": [],
    }


def _comment(n, body=None, author=None, account=None, updated=None):
    created = f"2026-11-01T09:{n:02d}:00.000+0200"
    return {
        "id": str(100 + n),
        "author": {"displayName": author or f"Person {n}", "accountId": account or f"acct-{n}"},
        "created": created,
        "updated": updated or created,
        "body": body or doc(para(text(f"comment {n}"))),
    }


def _history(n, items, who="Malek (Basita AI)"):
    return {"id": str(n), "author": {"displayName": who}, "created": f"2026-11-0{n}T08:00:00.000+0200", "items": items}


def _responses(comments, updated=UPDATED, post=True, histories=(), fields=None):
    found = [
        (200, json.dumps({"fields": {"updated": updated}})),
        (200, json.dumps({"fields": fields or _fields(updated)})),
        (200, json.dumps({"total": len(comments), "comments": comments})),
        (200, json.dumps({"total": len(histories), "isLast": True, "values": list(histories)})),
    ]
    return found + ([(201, json.dumps({"id": "99"}))] if post else [])


def test_the_core_file_layout(tmp_path):
    comments = [_comment(n) for n in range(1, 4)]
    transport = FakeTransport(*_responses(comments))
    result = client(transport).jira_md("BASITA-7", tmp_path)
    path = tmp_path / "work" / "jira" / "BASITA-7.md"
    assert result == {"path": str(path), "changed": True, "commented": True}
    lines = path.read_text(encoding="utf-8").split("\n")
    assert lines[0] == "# BASITA-7: Snapshot the issue"
    assert lines[2].startswith(
        f"<!-- jira-md: https://basitasolutions.atlassian.net/browse/BASITA-7 · updated {UPDATED} · fetched 20"
    )
    assert lines[2].endswith("Z -->")
    body = "\n".join(lines)
    assert (
        "- **Status:** In Progress\n- **Type:** Task\n- **Priority:** High\n- **Assignee:** Malek (Basita AI)\n"
        "- **Reporter:** Ahmed Oraby\n- **Fix versions:** 0.4.0\n- **Labels:** agent-picked\n"
        "- **Parent:** BASITA-1: Agents read files\n"
    ) in body
    assert "## Description\n\n### Acceptance criteria\n\n- AC1 works\n" in body
    assert "## Links\n\n- blocks DFP-1: A (Done)\n- is blocked by DFP-2: B (To Do)\n" in body
    assert "## Sub-tasks" not in body  # empty sections are left out
    assert "## Current state" in body and "## Archive" in body
    assert "work/jira/BASITA-7.full.md holds all 3 comments verbatim" in body
    # Three comments, no rule matches: the last three are shown, the index is empty and left out.
    assert "## Comments (3 of 3 shown" in body and "## Other comments" not in body


def test_few_comments_are_all_shown_and_none_gives_no_section(tmp_path):
    transport = FakeTransport(*_responses([], updated="2026-11-08T00:00:00.000+0200"))
    client(transport).jira_md("BASITA-7", tmp_path)
    body = (tmp_path / "work/jira/BASITA-7.md").read_text(encoding="utf-8")
    assert "## Comments" not in body and "## Other comments" not in body


def test_unchanged_makes_exactly_one_get_and_writes_nothing(tmp_path):
    client(FakeTransport(*_responses([]))).jira_md("BASITA-7", tmp_path)
    path = tmp_path / "work" / "jira" / "BASITA-7.md"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    transport = FakeTransport((200, json.dumps({"fields": {"updated": UPDATED}})))
    result = client(transport).jira_md("BASITA-7", tmp_path)
    assert result == {"path": str(path), "changed": False, "commented": False}
    assert len(transport.calls) == 1 and transport.calls[0][0] == "GET"
    assert transport.calls[0][1].endswith("/rest/api/3/issue/BASITA-7?fields=updated")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["BASITA-7.full.md", "BASITA-7.md"]  # no temp file left


def test_a_snapshot_without_its_archive_is_rewritten_even_when_unchanged(tmp_path):
    # BASITA-219: files written before the archive existed have the same `updated`; they must not stay as they are.
    client(FakeTransport(*_responses([]))).jira_md("BASITA-7", tmp_path)
    (tmp_path / "work/jira/BASITA-7.full.md").unlink()
    transport = FakeTransport(*_responses([]))
    assert client(transport).jira_md("BASITA-7", tmp_path)["changed"] is True
    assert (tmp_path / "work/jira/BASITA-7.full.md").is_file()


def test_the_link_comment_is_posted_once_and_never_twice(tmp_path):
    transport = FakeTransport(*_responses([_comment(1)]))
    assert client(transport).jira_md("BASITA-7", tmp_path)["commented"] is True
    posts = [call for call in transport.calls if call[0] != "GET"]
    assert len(posts) == 1
    method, url, _, sent = posts[0]
    assert method == "POST" and url == COMMENT_URL
    written = _adf_text(sent["body"])
    assert "basita-ops/work/jira/BASITA-7.md" in written and "Jira هو المرجع" in written
    assert "https://github.com/basitasolutions/basita-ops/blob/main/work/jira/BASITA-7.md" in written
    assert written.rstrip().endswith("— Malek (Basita AI)")
    # Jira now holds that comment; the next change rewrites the file and posts nothing.
    again = [_comment(1), {"author": {"displayName": "Malek"}, "created": "x", "body": sent["body"]}]
    transport = FakeTransport(*_responses(again, updated="2026-11-08T00:00:00.000+0200", post=False))
    assert client(transport).jira_md("BASITA-7", tmp_path) == {
        "path": str(tmp_path / "work" / "jira" / "BASITA-7.md"),
        "changed": True,
        "commented": False,
    }
    assert {call[0] for call in transport.calls} == {"GET"}


def test_an_older_comment_with_the_marker_anywhere_suppresses_the_link_comment(tmp_path):
    marker = _comment(1, doc(para(text(f"see basita-ops/{snapshot_marker('BASITA-7')}"))))
    transport = FakeTransport(*_responses([marker, *[_comment(n) for n in range(2, 15)]], post=False))
    assert client(transport).jira_md("BASITA-7", tmp_path)["commented"] is False
    assert {call[0] for call in transport.calls} == {"GET"}


def test_a_failed_link_comment_says_the_file_was_written(tmp_path):
    transport = FakeTransport(*_responses([], post=False), (403, '{"message":"no"}'))
    with pytest.raises(AtlError, match="wrote .*BASITA-7.md, but not the link comment"):
        client(transport).jira_md("BASITA-7", tmp_path)
    assert (tmp_path / "work" / "jira" / "BASITA-7.md").is_file()


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_an_out_dir_in_the_app_repo_is_refused_before_any_call(tmp_path):
    repo = tmp_path / "app"
    (repo / "docs").mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("remote", "add", "origin", APP_HTTPS, cwd=repo)
    transport = FakeTransport()
    for out in (repo, repo / "docs"):
        with pytest.raises(AtlError, match="app repository"):
            client(transport).jira_md("BASITA-7", out)
    assert transport.calls == [] and not (repo / "work").exists()


def test_another_repo_or_one_without_origin_is_allowed(tmp_path):
    ops = tmp_path / "basita-ops"
    ops.mkdir()
    _git("init", "-q", cwd=ops)
    assert client(FakeTransport(*_responses([]))).jira_md("BASITA-7", ops)["changed"] is True
    _git("remote", "add", "origin", "https://github.com/basitasolutions/basita-ops.git", cwd=ops)
    transport = FakeTransport(*_responses([], updated="2026-11-08T00:00:00.000+0200"))
    assert client(transport).jira_md("BASITA-7", ops)["changed"] is True


def test_a_bad_key_or_a_missing_folder_is_refused(tmp_path):
    with pytest.raises(AtlError, match="not an issue key"):
        client(FakeTransport()).jira_md("BASITA-7/../../x", tmp_path)
    with pytest.raises(AtlError, match="no folder"):
        client(FakeTransport()).jira_md("BASITA-7", tmp_path / "missing")


# --- the CLI ------------------------------------------------------------------------------------------------------


def _cli(args, transport):
    return main([*args, "--cloud-id", CLOUD], token_source=lambda agent, product: TOKEN, transport=transport)


def test_cli_jira_md_prints_only_the_path(tmp_path, capsys):
    path = tmp_path / "work" / "jira" / "BASITA-7.md"
    assert _cli(["malek", "jira-md", "BASITA-7", "--out", str(tmp_path)], FakeTransport(*_responses([]))) == 0
    assert capsys.readouterr().out == f"{path}\n"
    unchanged = FakeTransport((200, json.dumps({"fields": {"updated": UPDATED}})))
    assert _cli(["malek", "jira-md", "BASITA-7", "--out", str(tmp_path)], unchanged) == 0
    assert capsys.readouterr().out == f"{path} (unchanged)\n"


def test_cli_jira_md_uses_basita_ops_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BASITA_OPS_DIR", str(tmp_path))
    assert _cli(["malek", "jira-md", "BASITA-7"], FakeTransport(*_responses([]))) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "work" / "jira" / "BASITA-7.md")


def test_cli_jira_get_prints_the_markdown(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BASITA_OPS_DIR", str(tmp_path))
    assert _cli(["malek", "jira-get", "BASITA-7"], FakeTransport(*_responses([_comment(1)]))) == 0
    out = capsys.readouterr().out
    assert out.startswith("# BASITA-7: Snapshot the issue\n") and "## Description" in out
    assert out == (tmp_path / "work" / "jira" / "BASITA-7.md").read_text(encoding="utf-8")


def test_cli_jira_get_without_the_folder_prints_from_memory_and_writes_nothing(tmp_path, monkeypatch, capsys):
    # A cloud routine has no basita-ops: jira-get still reads, with no file and no link comment.
    missing = tmp_path / "no-basita-ops"
    monkeypatch.setenv("BASITA_OPS_DIR", str(missing))
    found = _responses([_comment(1)], post=False)[1:]
    transport = FakeTransport(*found)
    assert _cli(["malek", "jira-get", "BASITA-7"], transport) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("# BASITA-7: Snapshot the issue\n") and "## Description" in captured.out
    assert "no basita-ops folder here; printed the ticket without saving a snapshot" in captured.err
    assert {call[0] for call in transport.calls} == {"GET"}
    assert not missing.exists()


def test_cli_jira_get_still_prints_when_the_link_comment_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BASITA_OPS_DIR", str(tmp_path))
    transport = FakeTransport(*_responses([], post=False), (403, '{"message":"no"}'))
    assert _cli(["malek", "jira-get", "BASITA-7"], transport) == 0
    captured = capsys.readouterr()
    assert captured.out == (tmp_path / "work" / "jira" / "BASITA-7.md").read_text(encoding="utf-8")
    assert "but not the link comment" in captured.err


def test_cli_jira_md_without_the_folder_still_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BASITA_OPS_DIR", str(tmp_path / "no-basita-ops"))
    assert _cli(["malek", "jira-md", "BASITA-7"], FakeTransport()) == 1
    assert "set BASITA_OPS_DIR or pass --out" in capsys.readouterr().err


def _json_responses():
    issue = {"fields": {**_fields(), "description": doc(para(text("Given")))}}
    return (
        (200, json.dumps(issue)),
        (200, json.dumps({"total": 1, "comments": [_comment(1)]})),
        (200, json.dumps({"issues": [{"key": "BASITA-7"}]})),
    )


def test_cli_jira_get_json_is_unchanged_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BASITA_OPS_DIR", str(tmp_path))
    transport = FakeTransport(*_json_responses())
    assert _cli(["malek", "jira-get", "BASITA-7", "--json"], transport) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == client(FakeTransport(*_json_responses())).jira_get("BASITA-7")
    assert printed["description"] == "Given" and printed["comments"][0]["text"] == "comment 1"
    assert {call[0] for call in transport.calls} == {"GET"} and list(tmp_path.iterdir()) == []


def test_cli_options_belong_to_their_commands(tmp_path, capsys):
    cases = (
        ["malek", "jira-md", "BASITA-7", "--json"],
        ["malek", "jira-get", "BASITA-7", "--json", "--out", str(tmp_path)],
        ["malek", "jira-jql", "project = DFP", "--out", str(tmp_path)],
        ["malek", "jira-md", "BASITA-7", "extra text"],
    )
    for args in cases:
        transport = FakeTransport()
        assert _cli(args, transport) == 2
        assert transport.calls == []
    err = capsys.readouterr().err
    assert "--json is for jira-get" in err and "--out is for jira-md and jira-get" in err


def test_cli_refuses_the_app_repo(tmp_path, capsys):
    _git("init", "-q", cwd=tmp_path)
    _git("remote", "add", "origin", APP_SSH, cwd=tmp_path)
    transport = FakeTransport()
    assert _cli(["malek", "jira-md", "BASITA-7", "--out", str(tmp_path)], transport) == 1
    assert "app repository" in capsys.readouterr().err and transport.calls == []


# --- BASITA-219: the snapshot covers what the Atlassian MCP read gave ----------------------------------------------

AHMED = "712020:710281c6-e4c2-44fd-b72a-81091935ae21"  # client.AHMED_ACCOUNT
ADAM = "712020:74556401-e243-4c53-9f59-75f28fdf6cec"  # client.ADAM_ACCOUNT


def _long_fields():
    return {
        **_fields(),
        "created": "2026-11-01T08:00:00.000+0200",
        "duedate": "2026-11-20",
        "resolution": {"name": "Done"},
        "components": [{"name": "atl"}, {"name": "pickup"}],
        "attachment": [
            {
                "filename": "plan.pdf",
                "mimeType": "application/pdf",
                "size": 2048,
                "author": {"displayName": "Yousef"},
                "created": "2026-11-02T09:00:00.000+0200",
            }
        ],
    }


HISTORIES = (
    _history(3, [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]),
    _history(
        2,
        [
            {"field": "labels", "fromString": "", "toString": "high-priority"},
            {"field": "description", "fromString": "old text " * 50, "toString": "new text " * 50},
        ],
        who="Sherif",
    ),
    _history(4, [{"field": "summary", "fromString": "x" * 200, "toString": None}], who="Ahmed Oraby"),
)


def _write(tmp_path, comments, **kwargs):
    client(FakeTransport(*_responses(comments, **kwargs))).jira_md("BASITA-7", tmp_path)
    folder = tmp_path / "work" / "jira"
    return (folder / "BASITA-7.md").read_text(encoding="utf-8"), (folder / "BASITA-7.full.md").read_text(
        encoding="utf-8"
    )


def _ids(text, pattern):
    return re.findall(pattern, text, re.MULTILINE)


def _mixed_comments():
    """Twelve comments: Ahmed's (101), a tagged one (103), a quote of Ahmed (105), a keyword (107), Adam's (109), a
    comment that is cited (110, by 112), an edited one (104), and plain ones."""
    comments = [_comment(n, author="Malek", account="m") for n in range(1, 13)]
    comments[0] = _comment(1, body=doc(para(text("Please do it this way."))), author="Ahmed Oraby", account=AHMED)
    comments[2] = _comment(3, body=doc(para(text("Handoff: ready for Salma"))), author="Malek", account="m")
    comments[4] = _comment(5, body=doc(para(text("Ahmed said: «ship it Friday»"))), author="Malek", account="m")
    comments[6] = _comment(7, body=doc(para(text("We decided to use the file."))), author="Malek", account="m")
    comments[8] = _comment(9, author="Adam", account=ADAM)
    comments[3] = _comment(4, updated="2026-11-01T11:00:00.000+0200", author="Malek", account="m")
    comments[11] = _comment(12, body=doc(para(text("as in comment 110"))), author="Malek", account="m")
    return comments


def test_the_core_file_pins_by_rule_and_shows_the_last_three(tmp_path):
    core, _ = _write(tmp_path, _mixed_comments())
    shown = _ids(core, r"^### #(\d+) ")
    # 101 Ahmed, 103 tag, 104 edited, 105 quote, 107 keyword, 109 Adam, 110 cited; 110-112 are the last three.
    assert shown == ["101", "103", "104", "105", "107", "109", "110", "111", "112"]
    assert "### #101 · 2026-11-01T09:01:00.000+0200 · Ahmed Oraby [pinned: author is Ahmed]" in core
    assert "[pinned: tag handoff]" in core and "[pinned: quotes Ahmed]" in core
    assert "[pinned: keyword decided]" in core and "[pinned: author is Adam]" in core
    assert "[pinned: edited after posting]" in core and "[pinned: cited by a later comment]" in core
    assert "## Comments (9 of 12 shown: pinned by rule, and the last 3; 3 more are in the index below)" in core
    # The rest are one line each in the index, with their place in the archive; plain comments are not in the core.
    index = _ids(core, r"^- #(\d+) · ")
    assert index == ["102", "106", "108"]
    assert "comment 2\n" not in core and "comment 6\n" not in core


def test_every_comment_is_in_the_core_or_the_index_and_the_archive_has_them_all(tmp_path):
    # BASITA-219 AC-G
    for day, comments in enumerate((_mixed_comments(), [_comment(n) for n in range(1, 40)], [_comment(1)]), start=1):
        core, full = _write(tmp_path, comments, updated=f"2026-11-0{day}T00:00:00.000+0200")
        wanted = {c["id"] for c in comments}
        in_core = set(_ids(core, r"^### #(\d+) "))
        in_index = set(_ids(core, r"^- #(\d+) · "))
        assert in_core | in_index == wanted and not in_core & in_index
        assert set(_ids(full, r"^### #(\d+) ")) == wanted


def test_the_index_points_at_the_archive_lines_that_hold_the_comment(tmp_path):
    core, full = _write(tmp_path, _mixed_comments())
    lines = full.split("\n")
    for number, start, end in re.findall(r"^- #(\d+) · .*\(archive lines (\d+)-(\d+)\)$", core, re.MULTILINE):
        chunk = lines[int(start) - 1 : int(end)]
        assert chunk[0].startswith(f"### #{number} · ") and chunk[-1] == f"comment {int(number) - 100}"
        assert not any(line.startswith("### #") for line in chunk[1:])


def test_the_archive_is_verbatim_and_ends_with_the_changelog(tmp_path):
    comments = [_comment(1, body=doc(para(text("first, long " * 40))))]
    _, full = _write(tmp_path, comments, histories=HISTORIES, fields=_long_fields())
    assert full.startswith("# BASITA-7: archive (every comment and the changelog)\n\n<!-- jira-md: ")
    assert ("first, long " * 40).strip() in full
    history = full[full.index("## History") :].split("\n")
    assert history[0] == "## History (4 changes, oldest first: when · who · field: from → to)"
    assert history[2:6] == [
        "- 2026-11-02T08:00:00.000+0200 · Sherif · labels: (empty) → high-priority",
        "- 2026-11-02T08:00:00.000+0200 · Sherif · description: changed",
        "- 2026-11-03T08:00:00.000+0200 · Malek (Basita AI) · status: To Do → In Progress",
        "- 2026-11-04T08:00:00.000+0200 · Ahmed Oraby · summary: " + "x" * 80 + " → (empty)",
    ]


def test_current_state_has_the_last_status_change_and_each_authors_latest_comment(tmp_path):
    long_text = "x" * 1500
    comments = [_comment(n, author="Sherif" if n == 1 else "Malek") for n in range(1, 8)]
    comments[0] = _comment(1, body=doc(para(text(long_text))), author="Sherif")
    core, _ = _write(tmp_path, comments, histories=HISTORIES)
    state = core[core.index("## Current state") : core.index("## Comments")]
    assert "- **Last status change:** To Do → In Progress, Malek (Basita AI), 2026-11-03T08:00:00.000+0200" in state
    assert "- **Last comment:** #107, Malek, 2026-11-01T09:07:00.000+0200" in state
    assert "- **Latest by Malek:** #107, shown below" in state
    assert "…full text: archive #101" in state and "x" * 1200 in state and "x" * 1201 not in state


def test_the_snapshots_own_link_comment_is_never_pinned_and_is_indexed(tmp_path):
    link = _comment(
        2,
        body=doc(
            para(text("ملف التيكت للـ agents: basita-ops/work/jira/BASITA-7.md (قرار decision) — Malek (Basita AI)"))
        ),
    )
    core, _ = _write(tmp_path, [_comment(1), link, *[_comment(n) for n in range(3, 8)]])
    assert "### #102 " not in core and "- #102 · " in core


def test_a_comment_another_one_replaces_is_indexed_as_superseded_but_ahmeds_never(tmp_path):
    old = _comment(1, body=doc(para(text("Decision: use A"))))
    ahmed = _comment(2, body=doc(para(text("Use B"))), author="Ahmed Oraby", account=AHMED)
    new = _comment(3, body=doc(para(text("Decision: replaces comment 101 and 102 with C"))))
    core, _ = _write(tmp_path, [old, ahmed, new, *[_comment(n) for n in range(4, 8)]])
    assert "[superseded by #103]" in core and "### #101 " not in core
    assert "### #102 " in core  # any doubt pins: Ahmed's comment stays


def test_a_signed_agent_comment_posted_from_ahmeds_account_is_not_his(tmp_path):
    posted = _comment(1, body=doc(para(text("done — Malek (Basita AI)"))), author="Ahmed Oraby", account=AHMED)
    core, _ = _write(tmp_path, [posted, *[_comment(n) for n in range(2, 8)]])
    assert "### #101 " not in core


def test_pinned_comments_over_the_cap_go_to_the_index_oldest_first_and_say_so(tmp_path):
    big = "decision " + "word " * 800
    comments = [_comment(n, body=doc(para(text(big)))) for n in range(1, 8)]
    core, _ = _write(tmp_path, comments)
    over = _ids(core, r"^- #(\d+) · .*\[pinned, over cap\]")
    assert over and over == sorted(over) and over[0] == "101"
    assert set(_ids(core, r"^### #(\d+) ")) | set(_ids(core, r"^- #(\d+) · ")) == {str(100 + n) for n in range(1, 8)}
    assert len(core.encode()) / 3 <= 8000 + 300  # under the cap; the last three always stay
    assert set(_ids(core, r"^### #(\d+) ")) >= {"105", "106", "107"}


def test_attachments_and_the_dates_are_in_the_core(tmp_path):
    core, _ = _write(tmp_path, [], fields=_long_fields())
    assert "- **Components:** atl, pickup\n- **Created:** 2026-11-01T08:00:00.000+0200\n" in core
    assert "- **Due date:** 2026-11-20\n- **Resolution:** Done\n" in core
    assert "## Attachments (names only; contents are not downloaded)\n\n" in core
    assert "- plan.pdf (application/pdf, 2048 bytes, Yousef, 2026-11-02T09:00:00.000+0200)\n" in core


def test_the_same_comments_give_the_same_files(tmp_path):
    first = _write(tmp_path, _mixed_comments())
    second = _write(tmp_path / "again", _mixed_comments()) if (tmp_path / "again").mkdir() is None else None
    strip = lambda text: re.sub(r"fetched \S+ -->", "fetched X -->", text)  # noqa: E731
    assert [strip(t) for t in first] == [strip(t) for t in second]


def test_every_changelog_page_is_read(tmp_path):
    first = {"total": 3, "isLast": False, "values": [HISTORIES[1], HISTORIES[0]]}
    second = {"total": 3, "isLast": True, "values": [HISTORIES[2]]}
    found = _responses([], post=True)[:3] + [(200, json.dumps(first)), (200, json.dumps(second)), (201, "{}")]
    transport = FakeTransport(*found)
    client(transport).jira_md("BASITA-7", tmp_path)
    assert transport.calls[3][1].endswith("/changelog?startAt=0&maxResults=100")
    assert transport.calls[4][1].endswith("/changelog?startAt=2&maxResults=100")
    assert (tmp_path / "work/jira/BASITA-7.full.md").read_text(encoding="utf-8").count("\n- 2026-11-0") == 4


def _get_responses(histories=(), comments=None):
    comments = [_comment(n) for n in range(1, 14)] if comments is None else comments
    found = [
        (200, json.dumps({"fields": {**_fields(), "description": doc(para(text("Given")))}})),
        (200, json.dumps({"total": len(comments), "comments": comments})),
    ]
    found.append((200, json.dumps({"issues": [{"key": "BASITA-7"}]})))  # the currentUser() search
    if histories:
        found.append((200, json.dumps({"total": len(histories), "isLast": True, "values": list(histories)})))
    return found


def test_jira_get_keeps_all_comments_and_can_limit_or_add_history():
    full = client(FakeTransport(*_get_responses())).jira_get("BASITA-7")
    assert len(full["comments"]) == 13 and "comments_total" not in full and "history" not in full
    last = client(FakeTransport(*_get_responses())).jira_get("BASITA-7", comments="3")
    assert [c["text"] for c in last["comments"]] == ["comment 11", "comment 12", "comment 13"]
    assert last["comments_total"] == 13
    none = client(FakeTransport(*_get_responses())).jira_get("BASITA-7", comments="0")
    assert none["comments"] == [] and none["comments_total"] == 13
    with_history = client(FakeTransport(*_get_responses(HISTORIES))).jira_get("BASITA-7", history=True)
    assert [(h["field"], h["to"]) for h in with_history["history"]] == [
        ("labels", "high-priority"),
        ("description", "changed"),
        ("status", "In Progress"),
        ("summary", None),
    ]
    with pytest.raises(AtlError, match="--comments takes all or a number"):
        client(FakeTransport()).jira_get("BASITA-7", comments="some")


def test_cli_jira_get_json_takes_comments_and_history(capsys):
    transport = FakeTransport(*_get_responses(HISTORIES))
    assert _cli(["malek", "jira-get", "BASITA-7", "--json", "--comments", "2", "--history"], transport) == 0
    printed = json.loads(capsys.readouterr().out)
    assert len(printed["comments"]) == 2 and printed["comments_total"] == 13 and len(printed["history"]) == 4
    assert {call[0] for call in transport.calls} == {"GET"}


def test_cli_jira_get_history_prints_the_lines_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BASITA_OPS_DIR", str(tmp_path))
    found = [(200, json.dumps({"total": 3, "isLast": True, "values": list(HISTORIES)}))]
    transport = FakeTransport(*found)
    assert _cli(["malek", "jira-get", "BASITA-7", "--history"], transport) == 0
    lines = capsys.readouterr().out.strip().split("\n")
    assert len(lines) == 4 and lines[2].endswith("Malek (Basita AI) · status: To Do → In Progress")
    assert list(tmp_path.iterdir()) == [] and len(transport.calls) == 1


def test_cli_history_and_comments_belong_to_jira_get(tmp_path, capsys):
    cases = (
        ["malek", "jira-md", "BASITA-7", "--history"],
        ["malek", "jira-jql", "project = DFP", "--comments", "3"],
        ["malek", "jira-get", "BASITA-7", "--comments", "3"],
        ["malek", "jira-get", "BASITA-7", "--history", "--out", str(tmp_path)],
    )
    for args in cases:
        transport = FakeTransport()
        assert _cli(args, transport) == 2
        assert transport.calls == []
    err = capsys.readouterr().err
    assert "--history and --comments are for jira-get" in err and "--comments is for jira-get --json" in err


def test_any_mention_of_ahmed_pins_and_so_does_a_dropped_scope(tmp_path):
    # The recall check on DFP-169 (#11208): "Ahmed dropped AC5" is a scope decision no verb list should miss.
    dropped = _comment(1, body=doc(para(text("Scope is now AC1-AC4 only: Ahmed dropped AC5."))), author="Malek")
    waiting = _comment(2, body=doc(para(text("Still waiting for your review."))), author="Malek")
    arabic = _comment(3, body=doc(para(text("أحمد طلب نبدأ بالتاني"))), author="Malek")
    core, _ = _write(tmp_path, [dropped, waiting, arabic, *[_comment(n) for n in range(4, 9)]])
    assert "### #101 · 2026-11-01T09:01:00.000+0200 · Malek [pinned: mentions Ahmed, keyword dropped]" in core
    assert "[pinned: keyword waiting for]" in core and "### #103 " in core


def test_over_the_cap_ahmeds_own_comment_stays_while_older_ones_leave(tmp_path):
    big = "decision " + "word " * 800
    comments = [_comment(n, body=doc(para(text(big)))) for n in range(1, 8)]
    comments[0] = _comment(1, body=doc(para(text(big))), author="Ahmed Oraby", account=AHMED)
    core, _ = _write(tmp_path, comments)
    over = _ids(core, r"^- #(\d+) · .*\[pinned, over cap\]")
    assert over and "101" not in over and "### #101 " in core  # the oldest pin is Ahmed's: it goes last
