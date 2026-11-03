"""atl confluence-attach, --label and --parent on confluence-create/edit, and jira-link without the link-type read
(BASITA-188). Offline: a fake transport that keeps the raw request bodies, so an upload can be inspected."""

import base64
import json

import pytest

from atl_agent import AtlClient, AtlError
from atl_agent.__main__ import main
from atl_agent.client import BUILTIN_LINK_TYPES, _check_confluence_labels, _multipart, _pick_link_type

TOKEN = "tok-" + "z" * 12  # assembled at runtime; not a real credential
CLOUD = "cloud-1"
SPACE = "98765"


class Recorder:
    """Returns the queued responses in order and keeps every call: (method, url, headers, raw body)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.responses.pop(0)

    def json(self, index):
        return json.loads(self.calls[index][3])

    @property
    def methods(self):
        return [c[0] for c in self.calls]

    @property
    def urls(self):
        return [c[1] for c in self.calls]


def client(transport, agent="malek"):
    return AtlClient(agent, CLOUD, TOKEN, transport=transport)


def ok(data=None):
    return (200, json.dumps(data if data is not None else {}))


def page(page_id="123", parent="50", space=SPACE, version=4, parent_type="page", body="<p>old</p>"):
    data = {
        "id": page_id,
        "title": "Plan",
        "spaceId": space,
        "parentId": parent,
        "parentType": parent_type,
        "version": {"number": version},
        "body": {"storage": {"value": body}},
    }
    return ok(data)


def run(args, transport, monkeypatch=None):
    return main([*args, "--cloud-id", CLOUD], token_source=lambda a, p: TOKEN, transport=transport)


# ---------- jira-link without the link-type read ----------

LINKED = (200, json.dumps({"fields": {"issuelinks": []}}))


def links_on(type_name, outward, inward, other, as_outward=True):
    side = "outwardIssue" if as_outward else "inwardIssue"
    found = {"type": {"name": type_name, "outward": outward, "inward": inward}, side: {"key": other}}
    return (200, json.dumps({"fields": {"issuelinks": [found]}}))


# (given, link type name, the inward phrase was used, the relation Jira shows on DFP-1)
BUILTIN_CASES = [
    ("relates to", "Relates", False, "relates to"),
    ("Relates", "Relates", False, "relates to"),
    ("RELATES TO", "Relates", False, "relates to"),
    ("blocks", "Blocks", False, "blocks"),
    ("Blocks", "Blocks", False, "blocks"),
    ("is blocked by", "Blocks", True, "is blocked by"),
    ("duplicates", "Duplicate", False, "duplicates"),
    ("Duplicate", "Duplicate", False, "duplicates"),
    ("is duplicated by", "Duplicate", True, "is duplicated by"),
    ("clones", "Cloners", False, "clones"),
    ("Cloners", "Cloners", False, "clones"),
    ("is cloned by", "Cloners", True, "is cloned by"),
]


@pytest.mark.parametrize("given, name, reverse, relation", BUILTIN_CASES)
def test_the_four_builtin_link_types_never_read_the_link_type_list(given, name, reverse, relation):
    kind = next(t for t in BUILTIN_LINK_TYPES if t["name"] == name)
    shown = links_on(name, kind["outward"], kind["inward"], "DFP-2", as_outward=not reverse)
    transport = Recorder(LINKED, (201, ""), shown)
    assert client(transport).jira_link_typed("DFP-1", "DFP-2", given) == relation
    assert not any("issueLinkType" in url for url in transport.urls)
    assert transport.methods == ["GET", "POST", "GET"]
    body = transport.json(1)
    assert body["type"] == {"name": name}
    first, second = ("DFP-2", "DFP-1") if reverse else ("DFP-1", "DFP-2")
    assert body["inwardIssue"] == {"key": first} and body["outwardIssue"] == {"key": second}


def test_the_builtin_table_picks_what_the_sites_own_list_would():
    """The same words pick the same type, in the same direction, as the read path does on a standard site (Jira lists
    its built-in types in its own order, with ids)."""
    by_name = {t["name"]: t for t in BUILTIN_LINK_TYPES}
    site = [dict(by_name[n], id=str(i)) for i, n in enumerate(("Blocks", "Cloners", "Duplicate", "Relates"))]
    words = {w for t in BUILTIN_LINK_TYPES for w in (t["name"], t["outward"], t["inward"])}
    for word in words | {w.upper() for w in words}:
        built_in, reverse = _pick_link_type(word.lower(), BUILTIN_LINK_TYPES)
        listed, listed_reverse = _pick_link_type(word.lower(), site)
        assert (built_in["name"], reverse) == (listed["name"], listed_reverse)
    assert _pick_link_type("causes", BUILTIN_LINK_TYPES) == (None, False)


def test_any_other_link_type_still_reads_the_site_list():
    custom = {"name": "Causes", "outward": "causes", "inward": "is caused by"}
    types = (200, json.dumps({"issueLinkTypes": [*BUILTIN_LINK_TYPES, custom]}))
    shown = links_on("Causes", "causes", "is caused by", "DFP-2", as_outward=False)
    transport = Recorder(types, LINKED, (201, ""), shown)
    assert client(transport).jira_link_typed("DFP-1", "DFP-2", "is caused by") == "is caused by"
    assert transport.urls[0].endswith("/rest/api/3/issueLinkType")
    assert transport.json(2)["type"] == {"name": "Causes"}


def test_an_unknown_link_type_reads_the_list_and_posts_nothing():
    types = (200, json.dumps({"issueLinkTypes": list(BUILTIN_LINK_TYPES)}))
    transport = Recorder(types)
    with pytest.raises(AtlError, match="'causes' is not a link type .*Blocks \\(blocks / is blocked by\\)"):
        client(transport).jira_link_typed("DFP-1", "DFP-2", "causes")
    assert transport.methods == ["GET"]


def test_cli_jira_link_without_type_reads_no_link_type_list(capsys):
    transport = Recorder(LINKED, (201, ""), links_on("Relates", "relates to", "relates to", "ELB-3"))
    assert run(["sherif", "jira-link", "DFP-1", "ELB-3"], transport) == 0
    assert capsys.readouterr().out.strip() == "DFP-1 relates to ELB-3, by Sherif"
    assert not any("issueLinkType" in url for url in transport.urls)


# ---------- labels ----------


@pytest.mark.parametrize(
    "bad", ["", " ", "Has Space", "two words", "UPPER", "Mixed", "a,b", "a.b", "a&b", "a/b", "a(b)", "a!", "x" * 256]
)
def test_bad_labels_are_refused(bad):
    with pytest.raises(AtlError, match="is not a Confluence label"):
        _check_confluence_labels([bad])


def test_good_labels_pass_and_repeats_collapse():
    assert _check_confluence_labels(["wow", "wow", "process-docs", "v2_0", "عربي"]) == [
        "wow",
        "process-docs",
        "v2_0",
        "عربي",
    ]


def test_create_adds_the_labels_after_the_page_exists():
    transport = Recorder(ok({"results": [{"id": SPACE}]}), ok({"id": "200", "version": {"number": 1}}), ok())
    result = client(transport).confluence_create("DFP", "T", "text", labels=("wow", "docs"))
    assert transport.methods == ["GET", "POST", "POST"]
    assert transport.urls[1].endswith("/wiki/api/v2/pages")
    assert transport.urls[2].endswith("/wiki/rest/api/content/200/label")
    assert transport.json(2) == [{"prefix": "global", "name": "wow"}, {"prefix": "global", "name": "docs"}]
    assert result["labels_added"] == ["wow", "docs"]


def test_create_with_a_bad_label_calls_nothing():
    transport = Recorder()
    with pytest.raises(AtlError, match="not a Confluence label"):
        client(transport).confluence_create("DFP", "T", "text", labels=("Bad Label",))
    assert transport.calls == []


def test_create_names_the_page_when_the_labels_fail():
    transport = Recorder(ok({"results": [{"id": SPACE}]}), ok({"id": "200"}), (403, "no scope"))
    with pytest.raises(AtlError, match="page 200 was created, but the labels were not added: .*HTTP 403"):
        client(transport).confluence_create("DFP", "T", "text", labels=("wow",))


def test_edit_with_text_and_labels_edits_then_labels():
    transport = Recorder(page(), ok({"id": "123", "version": {"number": 5}}), ok())
    result = client(transport).confluence_edit("123", "new", labels=("wow",))
    assert transport.methods == ["GET", "PUT", "POST"]
    assert transport.urls[2].endswith("/wiki/rest/api/content/123/label")
    assert result["labels_added"] == ["wow"]


def test_edit_with_only_labels_makes_no_new_page_version():
    transport = Recorder(ok())
    result = client(transport).confluence_edit("123", labels=("wow", "docs"))
    assert transport.methods == ["POST"]
    assert transport.urls[0].endswith("/wiki/rest/api/content/123/label")
    assert result == {"id": "123", "labels_added": ["wow", "docs"]}


def test_edit_with_a_bad_label_calls_nothing():
    transport = Recorder()
    with pytest.raises(AtlError, match="not a Confluence label"):
        client(transport).confluence_edit("123", "new", labels=("ok", "Not OK"))
    assert transport.calls == []


def test_edit_names_the_page_when_the_labels_fail():
    transport = Recorder(page(), ok({"id": "123"}), (403, "no scope"))
    with pytest.raises(AtlError, match="page 123 was edited, but the labels were not added"):
        client(transport).confluence_edit("123", "new", labels=("wow",))


def test_edit_with_nothing_to_change_is_still_refused():
    with pytest.raises(AtlError, match="nothing to change"):
        client(Recorder()).confluence_edit("123")


def test_cli_labels_on_create_and_label_only_edit(capsys):
    transport = Recorder(ok({"results": [{"id": SPACE}]}), ok({"id": "200", "version": {"number": 1}}), ok())
    assert (
        run(["malek", "confluence-create", "DFP", "text", "--title", "T", "--label", "a", "--label", "b"], transport)
        == 0
    )
    assert "page 200 version 1 by Malek" in capsys.readouterr().out
    assert transport.json(2) == [{"prefix": "global", "name": "a"}, {"prefix": "global", "name": "b"}]
    transport = Recorder(ok())
    assert run(["malek", "confluence-edit", "123", "--label", "a"], transport) == 0
    assert capsys.readouterr().out.strip() == "page 123 labels added: a"


def test_cli_label_is_refused_where_it_means_nothing(capsys):
    transport = Recorder()
    assert run(["malek", "confluence-append", "123", "text", "--label", "a"], transport) == 2
    assert "--label is for" in capsys.readouterr().err
    assert transport.calls == []


# ---------- moving a page ----------


def test_move_puts_the_parent_on_the_same_page_id_and_keeps_the_body():
    transport = Recorder(
        page(parent="50"),  # the page
        page("456", parent="10"),  # the new parent
        page("10", parent=None),  # its parent: the top
        ok({"id": "123", "version": {"number": 5}}),
    )
    result = client(transport).confluence_edit("123", parent="456")
    assert transport.methods == ["GET", "GET", "GET", "PUT"]
    method, url, _, _ = transport.calls[3]
    assert method == "PUT" and url.endswith("/wiki/api/v2/pages/123")
    body = transport.json(3)
    assert body["id"] == "123" and body["parentId"] == "456" and body["title"] == "Plan"
    assert body["body"] == {"representation": "storage", "value": "<p>old</p>"}
    assert body["version"] == {"number": 5, "message": "[Malek] edited: body (moved under page 456)"}
    assert result["version"]["number"] == 5


def test_move_with_new_text_and_title_and_a_url_parent():
    transport = Recorder(
        page(),
        page("456", parent=None),
        ok({"id": "123", "version": {"number": 5}}),
    )
    url = "https://basitasolutions.atlassian.net/wiki/spaces/DFP/pages/456/Parent+page"
    client(transport).confluence_edit("123", "fresh text", title="New", parent=url)
    body = transport.json(2)
    assert body["parentId"] == "456" and body["title"] == "New"
    assert body["body"]["representation"] == "atlas_doc_format"
    assert body["version"]["message"] == "[Malek] fresh text (moved under page 456)"


def test_edit_without_parent_sends_no_parent_id():
    transport = Recorder(page(), ok({"id": "123"}))
    client(transport).confluence_edit("123", "new")
    assert "parentId" not in transport.json(1)


def test_move_to_another_space_is_refused_before_any_write():
    transport = Recorder(page(space="1"), page("456", space="2"))
    with pytest.raises(AtlError, match="in another space .*only within its space"):
        client(transport).confluence_edit("123", "new text", parent="456")
    assert transport.methods == ["GET", "GET"]


@pytest.mark.parametrize("missing", ["page", "parent"])
def test_move_refuses_when_a_space_cannot_be_read(missing):
    transport = Recorder(
        page(space=None if missing == "page" else SPACE), page("456", space=None if missing == "parent" else SPACE)
    )
    with pytest.raises(AtlError, match="can't tell which space"):
        client(transport).confluence_edit("123", parent="456")
    assert "PUT" not in transport.methods


def test_move_under_itself_is_refused_without_a_call():
    transport = Recorder()
    with pytest.raises(AtlError, match="under itself"):
        client(transport).confluence_edit("123", parent="123")
    assert transport.calls == []


def test_move_under_its_own_child_is_refused_before_any_write():
    transport = Recorder(
        page(parent="50"),
        page("456", parent="300"),  # the new parent...
        page("300", parent="123"),  # ...is under 300, which is under the page itself
    )
    with pytest.raises(AtlError, match="under page 123: a page can't be moved under its own child"):
        client(transport).confluence_edit("123", parent="456")
    assert transport.methods == ["GET", "GET", "GET"]


def test_move_under_its_direct_child_is_refused():
    transport = Recorder(page(parent="50"), page("456", parent="123"))
    with pytest.raises(AtlError, match="own child"):
        client(transport).confluence_edit("123", parent="456")
    assert "PUT" not in transport.methods


def test_move_stops_looking_up_at_a_folder_or_the_top():
    transport = Recorder(
        page(), page("456", parent="999", parent_type="folder"), ok({"id": "123", "version": {"number": 5}})
    )
    client(transport).confluence_edit("123", parent="456")
    assert transport.methods == ["GET", "GET", "PUT"]


def test_move_gives_up_on_a_very_deep_hierarchy(monkeypatch):
    monkeypatch.setattr("atl_agent.client.MOVE_DEPTH_CAP", 3)
    transport = Recorder(page(), *[page(str(400 + i), parent=str(401 + i)) for i in range(10)])
    with pytest.raises(AtlError, match="more than 3 levels deep"):
        client(transport).confluence_edit("123", parent="400")
    assert "PUT" not in transport.methods


def test_move_to_the_parent_it_already_has_is_nothing_to_change():
    transport = Recorder(page(parent="50"))
    with pytest.raises(AtlError, match="already under page 50: nothing to change"):
        client(transport).confluence_edit("123", parent="50")
    assert transport.methods == ["GET"]


def test_move_to_the_same_parent_with_labels_only_adds_the_labels():
    transport = Recorder(page(parent="50"), ok())
    result = client(transport).confluence_edit("123", parent="50", labels=("wow",))
    assert transport.methods == ["GET", "POST"]
    assert result == {"id": "123", "labels_added": ["wow"]}


def test_move_with_something_that_is_not_a_page_id_calls_nothing():
    transport = Recorder()
    with pytest.raises(AtlError, match="not a Confluence page id or URL"):
        client(transport).confluence_edit("123", parent="the parent")
    assert transport.calls == []


def test_a_parent_that_cannot_be_read_stops_the_move():
    transport = Recorder(page(), (404, "{}"))
    with pytest.raises(AtlError, match="HTTP 404"):
        client(transport).confluence_edit("123", parent="456")
    assert transport.methods == ["GET", "GET"]


def test_cli_edit_with_only_a_parent_moves_the_page(capsys):
    transport = Recorder(page(), page("456", parent=None), ok({"id": "123", "version": {"number": 5}}))
    assert run(["malek", "confluence-edit", "123", "--parent", "456"], transport) == 0
    assert capsys.readouterr().out.strip() == "page 123 version 5 by Malek"
    assert transport.json(2)["parentId"] == "456"


# ---------- confluence-attach ----------


def attached(name="shot.png", item_id="att7", version=1):
    return ok({"results": [{"id": item_id, "title": name, "version": {"number": version}}]})


def make_file(tmp_path, name="shot.png", data=b"\x89PNG\r\n\x1a\n\x00\xff binary \r\n--not-a-boundary"):
    path = tmp_path / name
    path.write_bytes(data)
    return path, data


def test_attach_uploads_the_file_as_multipart_with_the_nocheck_header(tmp_path):
    path, data = make_file(tmp_path)
    transport = Recorder(attached())
    result = client(transport).confluence_attach("123", str(path))
    method, url, headers, body = transport.calls[0]
    assert method == "PUT"
    assert url == "https://api.atlassian.com/ex/confluence/cloud-1/wiki/rest/api/content/123/child/attachment"
    assert headers["X-Atlassian-Token"] == "nocheck"
    assert base64.b64decode(headers["Authorization"].split()[1]).decode() == f"malek@basitasolutions.com:{TOKEN}"
    kind, _, boundary = headers["Content-Type"].partition("; boundary=")
    assert kind == "multipart/form-data" and boundary
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="file"; filename="shot.png"\r\nContent-Type: image/png\r\n\r\n' + data + b"\r\n--" in body
    assert b'name="minorEdit"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\ntrue\r\n' in body
    assert b"[Malek] uploaded with atl" in body
    assert TOKEN.encode() not in body
    assert result == {
        "id": "att7",
        "name": "shot.png",
        "version": 1,
        "ref": '<ac:image><ri:attachment ri:filename="shot.png"/></ac:image>',
    }


def test_attach_sends_only_the_base_name_and_the_types_own_content_type(tmp_path):
    folder = tmp_path / "deep" / "dir"
    folder.mkdir(parents=True)
    path, _ = make_file(folder, "Notes.CSV", b"a,b")
    transport = Recorder(attached("Notes.CSV"))
    client(transport).confluence_attach("123", str(path))
    body = transport.calls[0][3]
    assert b'filename="Notes.CSV"\r\nContent-Type: text/csv' in body
    assert b"deep" not in body


def test_attach_of_a_name_the_page_has_reports_the_new_version(tmp_path, capsys):
    path, _ = make_file(tmp_path)
    transport = Recorder(attached(version=3))
    assert run(["malek", "confluence-attach", "123", str(path)], transport) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "attachment shot.png (id att7, version 3) on page 123, by Malek"
    assert out[1] == 'use: <ac:image><ri:attachment ri:filename="shot.png"/></ac:image>'
    assert transport.methods == ["PUT"]


def test_attach_takes_a_page_url_and_a_non_ascii_name(tmp_path):
    path, _ = make_file(tmp_path, "صورة & more.png")
    transport = Recorder(attached("صورة & more.png"))
    url = "https://basitasolutions.atlassian.net/wiki/spaces/DFP/pages/123/Title"
    result = client(transport).confluence_attach(url, str(path))
    assert transport.urls[0].endswith("/content/123/child/attachment")
    assert 'filename="صورة & more.png"'.encode() in transport.calls[0][3]
    assert result["ref"] == '<ac:image><ri:attachment ri:filename="صورة &amp; more.png"/></ac:image>'


def test_attach_uses_the_name_confluence_answers_with(tmp_path):
    path, _ = make_file(tmp_path)
    transport = Recorder(attached("shot-1.png"))
    assert client(transport).confluence_attach("123", str(path))["name"] == "shot-1.png"


def test_attach_a_body_that_is_not_the_results_list_still_reads(tmp_path):
    path, _ = make_file(tmp_path)
    transport = Recorder(ok({"id": "att9", "title": "shot.png", "version": {"number": 2}}))
    assert client(transport).confluence_attach("123", str(path))["id"] == "att9"


def test_attach_refuses_a_missing_file_a_directory_and_a_big_file_before_any_call(tmp_path, monkeypatch):
    transport = Recorder()
    with pytest.raises(AtlError, match="no such file"):
        client(transport).confluence_attach("123", str(tmp_path / "nope.png"))
    (tmp_path / "folder.png").mkdir()
    with pytest.raises(AtlError, match="is not a file"):
        client(transport).confluence_attach("123", str(tmp_path / "folder.png"))
    path, _ = make_file(tmp_path, "big.pdf", b"x" * 2048)
    monkeypatch.setattr("atl_agent.client.MAX_ATTACHMENT_BYTES", 1024)
    with pytest.raises(AtlError, match="big.pdf is 0.0 MB; atl uploads at most 0 MB"):
        client(transport).confluence_attach("123", str(path))
    assert transport.calls == []


def test_attach_cap_is_25_mb_and_the_message_says_so(tmp_path, monkeypatch):
    from atl_agent import client as module

    assert module.MAX_ATTACHMENT_BYTES == 25 * 1024 * 1024
    path, _ = make_file(tmp_path, "big.pdf", b"x" * 4096)
    monkeypatch.setattr(module, "MAX_ATTACHMENT_BYTES", 2048)
    with pytest.raises(AtlError, match="at most"):
        client(Recorder()).confluence_attach("123", str(path))


@pytest.mark.parametrize("name", ['say "hi".png', "back\\slash.png", "line\nbreak.png"])
def test_attach_refuses_names_that_would_break_the_upload(tmp_path, name):
    path, _ = make_file(tmp_path, name)
    transport = Recorder()
    with pytest.raises(AtlError, match="can't be uploaded"):
        client(transport).confluence_attach("123", str(path))
    assert transport.calls == []


def test_attach_refuses_what_is_not_a_page(tmp_path):
    path, _ = make_file(tmp_path)
    with pytest.raises(AtlError, match="not a Confluence page id or URL"):
        client(Recorder()).confluence_attach("a page", str(path))


def test_attach_explains_a_scope_error_without_the_token(tmp_path):
    path, _ = make_file(tmp_path)
    transport = Recorder((403, '{"message":"scope does not match"}'))
    with pytest.raises(AtlError) as caught:
        client(transport).confluence_attach("123", str(path))
    assert "HTTP 403" in str(caught.value) and "scopes" in str(caught.value) and TOKEN not in str(caught.value)


def test_cli_attach_needs_a_page_and_a_file_and_no_other_options(tmp_path, capsys):
    path, _ = make_file(tmp_path)
    for args in (
        ["malek", "confluence-attach", "123"],
        ["malek", "confluence-attach", "123", str(path), "--label", "a"],
        ["malek", "confluence-attach", "123", str(path), "--parent", "5"],
        ["malek", "confluence-attach", "123", str(path), "--title", "T"],
    ):
        transport = Recorder()
        assert run(args, transport) == 2
        assert transport.calls == []
    assert "confluence-attach takes a page and a file" in capsys.readouterr().err


def test_cli_attach_failure_is_a_one_line_error(tmp_path, capsys):
    transport = Recorder()
    assert run(["malek", "confluence-attach", "123", str(tmp_path / "gone.png")], transport) == 1
    assert capsys.readouterr().err.startswith("atl: no such file")


def test_cli_attach_uses_the_confluence_token(tmp_path):
    path, _ = make_file(tmp_path)
    seen = []

    def token(agent, product):
        seen.append(product)
        return TOKEN

    assert main(["malek", "confluence-attach", "123", str(path), "--cloud-id", CLOUD], token, Recorder(attached())) == 0
    assert seen == ["confluence"]


def test_the_multipart_boundary_is_not_in_the_file():
    for _ in range(20):
        content_type, body = _multipart({"a": "b"}, "f.bin", "application/octet-stream", b"--atl- data")
        boundary = content_type.split("boundary=")[1]
        assert body.count(f"--{boundary}".encode()) == 3  # two parts and the closing line


# ---------- nothing deletes ----------


def test_none_of_the_new_paths_sends_a_delete(tmp_path):
    path, _ = make_file(tmp_path)
    transport = Recorder(
        attached(),
        page(),
        page("456", parent=None),
        ok({"id": "123", "version": {"number": 5}}),
        ok(),
    )
    c = client(transport)
    c.confluence_attach("123", str(path))
    c.confluence_edit("123", "t", parent="456", labels=("wow",))
    assert set(transport.methods) <= {"GET", "POST", "PUT"}


# ---------- what confluence-attach may send (Yousef's review of #41) ----------

SECRET = b"-----" + b"BEGIN SECRET-----\nSECRET-VALUE-123\n"  # assembled at runtime; a stand-in, not a real key


@pytest.fixture
def never_read(monkeypatch):
    """A refusal must come from the path alone: reading any file fails the test."""

    def boom(self):
        raise AssertionError(f"{self} was read")

    monkeypatch.setattr("pathlib.Path.read_bytes", boom)


def refused(path, why):
    transport = Recorder()
    with pytest.raises(AtlError, match=why) as caught:
        client(transport).confluence_attach("123", str(path))
    assert transport.calls == []
    assert "SECRET-VALUE-123" not in str(caught.value)  # the reason names the rule, never the contents
    return str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        "server.pem",
        "id_ed25519.key",
        "cert.p12",
        "prod.env",
        "creds.json",
        "app.conf",
        "notes.txt",
        "README",  # no extension
        "id_rsa",  # no extension
        "backup.zip",
        "deploy.sh",
        "shot.png.pem",  # only the last extension counts
    ],
)
def test_attach_refuses_every_type_not_on_the_list(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(SECRET)
    message = refused(path, r"refused: atl attaches only png, jpg, jpeg, gif, webp, svg, pdf, docx, xlsx, pptx, csv")
    assert name in message


@pytest.mark.parametrize("name", [".env", ".shot.png", ".netrc"])
def test_attach_refuses_a_dotfile(tmp_path, name, never_read):
    path = tmp_path / name
    path.write_text("SECRET-VALUE-123")
    refused(path, "refused: atl attaches no dotfile and nothing in a dot folder")


def test_attach_refuses_an_image_in_a_dot_folder(tmp_path, never_read):
    folder = tmp_path / "work" / ".secrets"
    folder.mkdir(parents=True)
    path, _ = make_file(folder, "shot.png")
    refused(path, r"'\.secrets'")


@pytest.mark.parametrize(
    "folder", [".config/basita", ".ssh", ".gnupg", ".aws", ".claude/projects", "Library/Keychains"]
)
def test_attach_refuses_anything_in_the_secret_home_folders(tmp_path, monkeypatch, folder, never_read):
    home = tmp_path / "home"
    (home / folder).mkdir(parents=True)
    path, _ = make_file(home / folder, "shot.png")
    monkeypatch.setenv("HOME", str(home))
    why = "under ~/Library" if folder.startswith("Library") else "dot folder"
    refused(path, why)


def test_attach_follows_a_symlink_to_what_it_points_to(tmp_path, monkeypatch, never_read):
    home = tmp_path / "home"
    (home / "Library").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / "Library" / "real.png").write_bytes(b"x")
    (home / ".ssh" / "id.png").write_bytes(b"x")
    (tmp_path / "secret.pem").write_bytes(SECRET)
    monkeypatch.setenv("HOME", str(home))
    links = {"to-pem.png": tmp_path / "secret.pem", "to-library.png": home / "Library" / "real.png"}
    links["to-ssh.png"] = home / ".ssh" / "id.png"
    expected = {"to-pem.png": r"secret\.pem has \.pem", "to-library.png": "under ~/Library", "to-ssh.png": "dot folder"}
    for name, target in links.items():
        (tmp_path / name).symlink_to(target)
        refused(tmp_path / name, expected[name])


def test_attach_refuses_a_pem_named_like_an_image_by_its_link_but_not_a_real_image(tmp_path):
    """The given name and the resolved one must both pass: an image linked as .pem is refused too."""
    image, _ = make_file(tmp_path, "real.png")
    (tmp_path / "looks.pem").symlink_to(image)
    refused(tmp_path / "looks.pem", r"looks\.pem has \.pem")


@pytest.mark.parametrize("ext", ["png", "JPG", "jpeg", "gif", "webp", "svg", "pdf", "docx", "xlsx", "pptx", "csv"])
def test_attach_allows_each_listed_type_with_its_own_content_type(tmp_path, ext):
    from atl_agent.client import ATTACH_TYPES

    path, _ = make_file(tmp_path, f"file.{ext}", b"data")
    transport = Recorder(attached(f"file.{ext}"))
    client(transport).confluence_attach("123", str(path))
    assert f'filename="file.{ext}"\r\nContent-Type: {ATTACH_TYPES[ext.lower()]}'.encode() in transport.calls[0][3]


def test_attach_allows_an_image_elsewhere_in_the_home_folder(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Code" / "basita-ops" / "work").mkdir(parents=True)
    path, _ = make_file(home / "Code" / "basita-ops" / "work", "flow.png")
    monkeypatch.setenv("HOME", str(home))
    transport = Recorder(attached("flow.png"))
    assert client(transport).confluence_attach("123", str(path))["name"] == "flow.png"


def test_cli_attach_refusal_is_a_one_line_error(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_bytes(SECRET)
    monkeypatch.setenv("HOME", str(home))
    transport = Recorder()
    assert run(["malek", "confluence-attach", "123", "~/.ssh/id_ed25519"], transport) == 1
    err = capsys.readouterr().err
    assert err.startswith("atl: refused: atl attaches no dotfile") and "SECRET-VALUE-123" not in err
    assert transport.calls == []
