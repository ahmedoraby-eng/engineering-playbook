import io
import json

from atl_agent import AtlClient
from atl_agent.close import close_issues, closing_keys, main

TOKEN = "tok-" + "x" * 12  # assembled at runtime; not a real credential


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.split("/cloud-1")[1], json.loads(body) if body else None))
        return self.responses.pop(0)


def status(name, category):
    return (200, json.dumps({"fields": {"status": {"name": name, "statusCategory": {"key": category}}}}))


def transitions(*pairs):
    return (
        200,
        json.dumps(
            {
                "transitions": [
                    {"id": tid, "to": {"name": name, "statusCategory": {"key": cat}}} for tid, name, cat in pairs
                ]
            }
        ),
    )


def client(transport):
    return AtlClient("talia", "cloud-1", TOKEN, transport=transport)


def test_only_closes_counts_and_comments_are_ignored():
    body = """Closes BASITA-1
    Refs DFP-12
    <!-- Closes ELB-9 -->
    closes: dfp-3 and Closes BASITA-1 again, Closes OTHER-5, Closes <KEY>, Disclosures BASITA-8
    """
    assert closing_keys(body) == ["BASITA-1", "DFP-3"]
    assert closing_keys("") == [] and closing_keys(None) == []


def test_close_moves_to_done_and_comments_with_links():
    transport = FakeTransport(
        status("To Do", "new"),
        transitions(("11", "In Progress", "indeterminate"), ("31", "Done", "done")),
        (204, ""),
        (201, json.dumps({"id": "5"})),
    )
    lines = close_issues(client(transport), ["BASITA-1"], "https://github.com/o/r/pull/7", "- o/r#7 merged")
    assert lines == ["BASITA-1: To Do -> Done"]
    assert transport.calls[2] == ("POST", "/rest/api/3/issue/BASITA-1/transitions", {"transition": {"id": "31"}})
    comment = transport.calls[3][2]["body"]["content"]
    assert "https://github.com/o/r/pull/7 was merged" in comment[0]["content"][0]["text"]
    assert any("o/r#7 merged" in p.get("content", [{}])[0].get("text", "") for p in comment)


def test_already_done_is_left_alone():
    transport = FakeTransport(status("Done", "done"))
    assert close_issues(client(transport), ["DFP-2"], "u") == ["DFP-2: already Done, nothing to do"]
    assert len(transport.calls) == 1


def test_no_done_transition_is_an_error(capsys):
    transport = FakeTransport(status("To Do", "new"), transitions(("11", "In Progress", "indeterminate")))
    code = main(
        ["--pr-url", "u"],
        stdin=io.StringIO("Closes BASITA-1"),
        environ={"ATL_CLOUD_ID": "cloud-1", "ATL_TOKEN": TOKEN},
        transport=transport,
    )
    assert code == 1
    assert "no transition to a Done status" in capsys.readouterr().err


def test_main_without_keys_or_dry_run_touches_nothing(capsys):
    assert main(["--pr-url", "u"], stdin=io.StringIO("Refs BASITA-1")) == 0
    assert "nothing to close" in capsys.readouterr().out
    assert main(["--pr-url", "u", "--dry-run"], stdin=io.StringIO("Closes DFP-4")) == 0
    assert "Would close: DFP-4" in capsys.readouterr().out


def test_main_closes_and_reads_the_links_file(tmp_path, capsys):
    links = tmp_path / "links.txt"
    links.write_text("- a/b#1 merged\n")
    transport = FakeTransport(
        status("In Progress", "indeterminate"), transitions(("31", "Done", "done")), (204, ""), (201, "{}")
    )
    code = main(
        ["--pr-url", "u", "--links-file", str(links)],
        stdin=io.StringIO("Closes ELB-7"),
        environ={"ATL_CLOUD_ID": "cloud-1", "ATL_TOKEN": TOKEN},
        transport=transport,
    )
    assert code == 0 and "ELB-7: In Progress -> Done" in capsys.readouterr().out
