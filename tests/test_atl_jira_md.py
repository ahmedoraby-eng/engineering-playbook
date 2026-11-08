"""BASITA-205 (ADR-B001): the ADF -> Markdown renderer, `jira_md` snapshots and the jira-md / jira-get CLI."""

import json
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


def _comment(n, body=None):
    return {
        "author": {"displayName": f"Person {n}"},
        "created": f"2026-11-0{1 + n % 7}T09:00:00.000+0200",
        "body": body or doc(para(text(f"comment {n}"))),
    }


def _responses(comments, updated=UPDATED, post=True):
    found = [
        (200, json.dumps({"fields": {"updated": updated}})),
        (200, json.dumps({"fields": _fields(updated)})),
        (200, json.dumps({"total": len(comments), "comments": comments})),
    ]
    return found + ([(201, json.dumps({"id": "99"}))] if post else [])


def test_the_file_layout_and_the_last_ten_comments(tmp_path):
    comments = [_comment(n) for n in range(1, 13)]
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
    assert "## Comments (last 10 of 12)" in body
    shown = [line for line in lines if line.startswith("### Person")]
    assert [line.split(" · ")[0] for line in shown] == [f"### Person {n}" for n in range(3, 13)]
    assert "comment 2\n" not in body and body.endswith("comment 12\n")


def test_few_comments_are_all_shown_and_none_gives_no_section(tmp_path):
    transport = FakeTransport(*_responses([_comment(1), _comment(2)]))
    client(transport).jira_md("BASITA-7", tmp_path)
    assert "## Comments (2)" in (tmp_path / "work/jira/BASITA-7.md").read_text(encoding="utf-8")
    transport = FakeTransport(*_responses([], updated="2026-11-08T00:00:00.000+0200"))
    client(transport).jira_md("BASITA-7", tmp_path)
    assert "## Comments" not in (tmp_path / "work/jira/BASITA-7.md").read_text(encoding="utf-8")


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
    assert sorted(p.name for p in path.parent.iterdir()) == ["BASITA-7.md"]  # no temp file left


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
