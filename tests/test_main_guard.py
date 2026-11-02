from main_guard import Push, check
from main_guard.__main__ import main

ALLOWED = frozenset({"owner-login"})


def push(*actors, forced=False):
    return Push("refs/heads/main", actors, forced, "a" * 40, "b" * 40)


def test_a_push_by_the_owner_passes():
    assert check(push("owner-login", "Owner-Login", ""), ALLOWED) == []


def test_a_push_by_anyone_else_is_an_error():
    findings = check(push("owner-login", "agent-app[bot]"), ALLOWED)
    assert [f.severity for f in findings] == ["error"]
    assert "updated by agent-app[bot] (aaaaaaa..bbbbbbb)" in findings[0].message


def test_a_push_naming_no_actor_is_an_error():
    assert [f.severity for f in check(push("", ""), ALLOWED)] == ["error"]


def test_a_force_push_by_the_owner_is_a_warning():
    findings = check(push("owner-login", forced=True), ALLOWED)
    assert [(f.severity, f.message) for f in findings] == [
        ("warning", "refs/heads/main was force-pushed by owner-login")
    ]


def test_command_line_exit_codes_and_summary(tmp_path, capsys):
    summary = tmp_path / "summary.md"
    ok = ["--allowed", "owner-login, other", "--actor", "owner-login", "--actor", "owner-login"]
    assert main([*ok, "--summary", str(summary)]) == 0
    assert "updated by an allowed actor" in summary.read_text()

    bad = ["--allowed", "owner-login", "--actor", "owner-login", "--actor", "x[bot]", "--forced", "true"]
    assert main([*bad, "--summary", str(summary)]) == 1
    out = capsys.readouterr().out
    assert "::error title=Main guard::refs/heads/main was updated by x[bot]" in out
    assert "::warning title=Main guard::refs/heads/main was force-pushed" in out
    assert "red: refs/heads/main was updated by x[bot]" in summary.read_text()
