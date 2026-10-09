import pytest

from ai_guard.cli import main
from ai_guard.policy import Policy, matches
from helpers import FAKE_AWS_KEY, diff_for, pr_body


def test_missing_policy_file_uses_defaults(tmp_path):
    assert Policy.load(tmp_path / "absent.toml") == Policy()


def test_policy_file_overrides_defaults(tmp_path):
    path = tmp_path / ".ai-guard.toml"
    path.write_text('[policy]\nmax_ai_changed_lines = 50\nprotected_globs = ["billing/*"]\n')
    policy = Policy.load(path)
    assert policy.max_ai_changed_lines == 50
    assert policy.protected_globs == ("billing/*",)


def test_unknown_policy_keys_are_rejected(tmp_path):
    path = tmp_path / ".ai-guard.toml"
    path.write_text("[policy]\nmax_lines = 5\n")
    with pytest.raises(ValueError, match="max_lines"):
        Policy.load(path)


def test_globs_match_full_path_or_file_name():
    assert matches("apps/api/src/routes/auth.ts", ("*auth*",))
    assert matches("tests/test_x.py", ("tests/*",))
    assert not matches("src/x.py", ("tests/*",))


def _write(tmp_path, diff, body):
    (tmp_path / "c.diff").write_text(diff)
    (tmp_path / "body.md").write_text(body)
    return [
        "--diff",
        str(tmp_path / "c.diff"),
        "--pr-body",
        str(tmp_path / "body.md"),
        "--policy",
        str(tmp_path / "none.toml"),
    ]


def test_cli_exits_zero_when_clean(tmp_path, capsys):
    args = _write(tmp_path, diff_for({"README.md": ["hi"]}), pr_body(no_ai=True))
    assert main(args) == 0
    assert "0 error(s)" in capsys.readouterr().out


def test_cli_emits_github_annotations_and_fails(tmp_path, capsys):
    args = _write(tmp_path, diff_for({"src/settings.py": [f"KEY = '{FAKE_AWS_KEY}'"]}), pr_body(no_ai=True))
    assert main([*args, "--format", "github"]) == 1
    out = capsys.readouterr().out
    assert "::error file=src/settings.py,line=1,title=no-secrets::" in out


def test_warn_only_mode_reports_but_never_fails(tmp_path, capsys):
    args = _write(tmp_path, diff_for({"src/settings.py": [f"KEY = '{FAKE_AWS_KEY}'"]}), pr_body())
    assert main([*args, "--warn-only", "--format", "github"]) == 0
    out = capsys.readouterr().out
    assert "::warning file=src/settings.py,line=1,title=no-secrets::" in out
    assert "0 error(s), 2 warning(s)" in out


def test_cli_reads_diff_from_stdin(tmp_path, monkeypatch, capsys):
    import io

    (tmp_path / "body.md").write_text(pr_body(no_ai=True))
    monkeypatch.setattr("sys.stdin", io.StringIO(diff_for({"README.md": ["hi"]})))
    assert main(["--diff", "-", "--pr-body", str(tmp_path / "body.md"), "--policy", str(tmp_path / "x.toml")]) == 0
