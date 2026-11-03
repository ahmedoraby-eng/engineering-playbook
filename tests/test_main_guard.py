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


# --- BASITA-162: Talia's App may update main only by merging a pull request ----------------------------------


def test_a_merge_only_actor_passes_on_a_pr_merge():
    push = Push("refs/heads/main", ("basita-talia[bot]",), before="a" * 40, after="b" * 40)
    assert check(push, frozenset({"ahmedoraby-eng"}), frozenset({"basita-talia[bot]"}), pr_merge=True) == []


def test_a_merge_only_actor_fails_on_a_plain_or_forced_push():
    talia = frozenset({"basita-talia[bot]"})
    plain = Push("refs/heads/main", ("basita-talia[bot]",))
    (finding,) = check(plain, frozenset({"ahmedoraby-eng"}), talia, pr_merge=False)
    assert finding.severity == "error" and "only by merging a pull request" in finding.message
    forced = Push("refs/heads/main", ("basita-talia[bot]",), forced=True)
    findings = check(forced, frozenset({"ahmedoraby-eng"}), talia, pr_merge=True)
    assert [f.severity for f in findings] == ["error", "warning"] and "force-pushed by" in findings[0].message


def test_merge_only_through_the_command_line(capsys):
    args = ["--allowed", "ahmedoraby-eng", "--merge-only", "basita-talia[bot]", "--actor", "basita-talia[bot]"]
    assert main([*args, "--pr-merge", "true"]) == 0
    assert main([*args, "--pr-merge", "false"]) == 1
    assert main(args) == 1  # the workflow didn't say it was a merge
    assert main(["--allowed", "ahmedoraby-eng", "--actor", "basita-talia[bot]", "--pr-merge", "true"]) == 1
    assert "only by merging a pull request" in capsys.readouterr().out
