import json
import subprocess

from agent_attribution import Commit, Config, PullRequest, check
from agent_attribution.__main__ import main
from agent_attribution.check import trailers

CONFIG = Config(
    agents=frozenset({"malek", "salma"}),
    agent_domain="example.com",
    humans=frozenset({"owner@example.org", "owner-login"}),
)
PR = PullRequest(title="DFP-1: change", branch="feat/x", body="", author_login="malek-bot[bot]")


def commit(email="malek@example.com", message="Fix\n\nAgent: Malek", **kw):
    return Commit(sha="abcdef1234", author_name="X", author_email=email, message=message, **kw)


def rules(findings):
    return [f.rule for f in findings]


def test_agent_commit_with_matching_trailer_passes():
    assert check([commit()], PR, CONFIG) == []


def test_agent_commit_without_trailer_fails():
    assert rules(check([commit(message="Fix")], PR, CONFIG)) == ["agent-trailer-missing"]


def test_agent_trailer_must_name_the_author():
    findings = check([commit(message="Fix\n\nAgent: Salma")], PR, CONFIG)
    assert rules(findings) == ["agent-trailer-mismatch"]
    assert findings[0].severity == "error"


def test_agent_domain_and_name_are_case_insensitive():
    assert check([commit(email="Malek@Example.COM", message="Fix\n\nAgent: malek")], PR, CONFIG) == []


def test_smart_commit_commands_are_rejected_for_agents():
    for message in ("DFP-1 #done", "DFP-1 fix #comment looks good", "DFP-1 #time 1h"):
        assert "smart-commit" in rules(check([commit(message=f"{message}\n\nAgent: Malek")], PR, CONFIG))


def test_pr_numbers_and_anchors_are_not_smart_commits():
    for message in ("DFP-1: fix (#154)", "DFP-1 see docs/a.md#section", "Fix colour &#x2014;"):
        assert check([commit(message=f"{message}\n\nAgent: Malek")], PR, CONFIG) == []


def test_human_commit_passes_and_ai_made_human_commit_is_a_warning():
    assert check([commit(email="owner@example.org", message="Fix")], PR, CONFIG) == []
    ai = commit(email="owner@example.org", message="Fix\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    findings = check([ai], PR, CONFIG)
    assert rules(findings) == ["agent-commit-as-human"]
    assert findings[0].severity == "warning"


def test_human_is_recognised_by_login():
    assert (
        check([commit(email="1+owner@users.noreply.github.com", message="Fix", author_login="owner-login")], PR, CONFIG)
        == []
    )


def test_unknown_author_fails():
    assert rules(check([commit(email="someone@else.org", message="Fix")], PR, CONFIG)) == ["unknown-author"]


def test_merge_commits_and_bots_are_skipped():
    assert check([commit(email="x@y.z", message="Merge", parent_count=2)], PR, CONFIG) == []
    assert check([commit(email="x@y.z", message="Bump", author_login="dependabot[bot]")], PR, CONFIG) == []


def test_missing_issue_key_warns_or_fails():
    pr = PullRequest(title="fix things", branch="fix/things")
    findings = check([commit()], pr, CONFIG)
    assert rules(findings) == ["issue-key"] and findings[0].severity == "warning"
    strict = Config(CONFIG.agents, CONFIG.agent_domain, CONFIG.humans, require_issue_key=True)
    assert check([commit()], pr, strict)[0].severity == "error"
    assert check([commit()], PullRequest(title="x", branch="DFP-9-x"), CONFIG) == []


def test_pr_opened_by_a_human_with_agent_commits_needs_an_agent_line():
    pr = PullRequest(title="DFP-1 x", author_login="owner-login")
    assert rules(check([commit()], pr, CONFIG)) == ["pr-agent-line"]
    assert (
        check([commit()], PullRequest(title="DFP-1 x", author_login="owner-login", body="Agent: Malek"), CONFIG) == []
    )


def test_trailers_read_only_the_last_paragraph():
    assert trailers("Agent: Malek") == {}
    assert trailers("Title\n\nAgent: Malek\nRequested-by: A <a@b>") == {"agent": ["Malek"], "requested-by": ["A <a@b>"]}


def _github_commit(email, message, login=None, parents=1):
    return {
        "sha": "0123456789",
        "commit": {"author": {"name": "N", "email": email}, "message": message},
        "author": {"login": login} if login else None,
        "parents": [{}] * parents,
    }


def test_cli_reads_github_json_and_sets_exit_code(tmp_path, capsys):
    commits = tmp_path / "commits.json"
    pr = tmp_path / "pr.json"
    pr.write_text(json.dumps({"title": "DFP-1 x", "head": {"ref": "b"}, "body": "", "user": {"login": "a[bot]"}}))
    base = ["--commits", str(commits), "--pr", str(pr), "--agents", "malek", "--agent-domain", "example.com"]

    commits.write_text(json.dumps([_github_commit("malek@example.com", "Fix\n\nAgent: Malek")]))
    assert main(base) == 0

    commits.write_text(json.dumps([_github_commit("malek@example.com", "Fix")]))
    assert main([*base, "--format", "github"]) == 1
    assert "::error title=agent-trailer-missing::" in capsys.readouterr().out
    assert main([*base, "--warn-only"]) == 0


def test_agent_git_sets_identity_and_trailers(tmp_path):
    script = __import__("pathlib").Path(__file__).resolve().parents[1] / "scripts" / "agent-git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path), "AGENT_MODEL": "m-1"}
    subprocess.run(
        [str(script), "Malek", "commit", "-q", "--allow-empty", "-m", "DFP-1: x"], cwd=repo, env=env, check=True
    )
    log = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>|%cn <%ce>|%B"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    author, committer, body = log.split("|", 2)
    assert author == "Malek (Basita AI) <malek@basitasolutions.com>" == committer
    assert "Agent: Malek" in body and "Requested-by: Ahmed Oraby" in body and "Agent-Model: m-1" in body
    bad = subprocess.run([str(script), "mallory", "status"], cwd=repo, env=env, capture_output=True, text=True)
    assert bad.returncode == 2 and "unknown agent" in bad.stderr
