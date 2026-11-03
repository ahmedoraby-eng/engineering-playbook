import json
import subprocess
from dataclasses import replace

import agent_attribution.__main__ as cli
from agent_attribution import Commit, Config, PullRequest, check
from agent_attribution.__main__ import DEFAULT_IDENTITIES, main
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
    # Malek has a GitHub App, so the address is its bot's (see config/github-identities.json).
    assert author == "Malek (Basita AI) <351705446+basita-malek[bot]@users.noreply.github.com>" == committer
    assert "Agent: Malek" in body and "Requested-by: Ahmed Oraby" in body and "Agent-Model: m-1" in body
    bad = subprocess.run([str(script), "mallory", "status"], cwd=repo, env=env, capture_output=True, text=True)
    assert bad.returncode == 2 and "unknown agent" in bad.stderr


# GitHub App bots (BASITA-149)

BOT_EMAIL = "351705446+basita-malek[bot]@users.noreply.github.com"
BOTS = {"basita-malek[bot]": "malek", "basita-salma[bot]": "salma"}
BOT_CONFIG = replace(CONFIG, bots=BOTS)
FAIL_CONFIG = replace(BOT_CONFIG, bot_severity="error")
BOT_PR = PullRequest(title="DFP-1: change", branch="DFP-1-x", body="", author_login="basita-malek[bot]")


def bot_commit(message="Fix\n\nAgent: Malek", email=BOT_EMAIL, login="basita-malek[bot]", **kw):
    return commit(email=email, message=message, author_login=login, **kw)


def test_bot_commit_with_matching_trailer_passes():
    assert check([bot_commit()], BOT_PR, BOT_CONFIG) == []
    assert check([bot_commit(login="Basita-Malek[bot]", message="Fix\n\nAgent: malek")], BOT_PR, BOT_CONFIG) == []


def test_bot_commit_trailer_missing_or_mismatched_warns_then_fails():
    for message, rule in (("Fix", "bot-trailer-missing"), ("Fix\n\nAgent: Salma", "bot-trailer-mismatch")):
        warned = check([bot_commit(message=message)], BOT_PR, BOT_CONFIG)
        assert rules(warned) == [rule] and warned[0].severity == "warning"
        assert "basita-malek[bot]" in warned[0].message
        failed = check([bot_commit(message=message)], BOT_PR, FAIL_CONFIG)
        assert rules(failed) == [rule] and failed[0].severity == "error"


def test_bot_commit_is_recognised_by_email_alone():
    findings = check([bot_commit(message="Fix", login="")], BOT_PR, BOT_CONFIG)
    assert rules(findings) == ["bot-trailer-missing"]
    assert check([bot_commit(login="")], BOT_PR, BOT_CONFIG) == []
    # Without the numeric id it is not GitHub's noreply form, so the author is unknown.
    other = check([bot_commit(email="basita-malek[bot]@users.noreply.github.com", login="")], BOT_PR, BOT_CONFIG)
    assert rules(other) == ["unknown-author"]


def test_unmapped_bots_and_merges_are_still_skipped():
    assert check([bot_commit(message="Bump", login="dependabot[bot]", email="x@y.z")], BOT_PR, BOT_CONFIG) == []
    haya = bot_commit(
        message="Fix", login="basita-haya[bot]", email="351706579+basita-haya[bot]@users.noreply.github.com"
    )
    assert check([haya], BOT_PR, BOT_CONFIG) == []
    assert check([bot_commit(message="Merge", parent_count=2)], BOT_PR, BOT_CONFIG) == []
    # Without a bot map (the old behaviour) our bot is skipped like any other.
    assert check([bot_commit(message="Fix")], BOT_PR, CONFIG) == []


def test_smart_commit_by_a_bot_is_an_error_even_in_warn_mode():
    findings = check([bot_commit(message="DFP-1 #done\n\nAgent: Malek")], BOT_PR, BOT_CONFIG)
    assert rules(findings) == ["smart-commit"] and findings[0].severity == "error"


def test_bot_pr_with_another_agents_line_warns():
    pr = replace(BOT_PR, body="Agent: Salma\n\nRefs DFP-1")
    findings = check([bot_commit()], pr, BOT_CONFIG)
    assert rules(findings) == ["pr-bot-mismatch"] and findings[0].severity == "warning"
    assert "basita-malek[bot]" in findings[0].message and "Salma" in findings[0].message
    assert check([bot_commit()], replace(BOT_PR, body="Agent: Malek"), BOT_CONFIG) == []
    assert check([bot_commit()], pr, FAIL_CONFIG)[0].severity == "error"


def test_bot_pr_with_another_agents_commits_warns_once():
    salma_bot = bot_commit(
        message="Fix\n\nAgent: Salma",
        login="basita-salma[bot]",
        email="351707078+basita-salma[bot]@users.noreply.github.com",
    )
    salma_domain = commit(email="salma@example.com", message="Fix\n\nAgent: Salma")
    findings = check([bot_commit(), salma_bot, salma_domain], BOT_PR, BOT_CONFIG)
    assert rules(findings) == ["pr-bot-mismatch"]
    assert "basita-malek[bot]" in findings[0].message and "Salma" in findings[0].message
    both = check([salma_domain], replace(BOT_PR, body="Agent: Salma"), BOT_CONFIG)
    assert rules(both) == ["pr-bot-mismatch", "pr-bot-mismatch"]


def test_bot_pr_needs_no_agent_line_but_human_pr_with_bot_commits_does():
    assert check([bot_commit()], BOT_PR, BOT_CONFIG) == []
    human_pr = replace(BOT_PR, author_login="owner-login")
    assert rules(check([bot_commit()], human_pr, BOT_CONFIG)) == ["pr-agent-line"]
    assert check([bot_commit()], replace(human_pr, body="Agent: Malek"), BOT_CONFIG) == []


def _bot_cli(tmp_path, message="Fix", pr_login="basita-malek[bot]"):
    commits = tmp_path / "commits.json"
    pr = tmp_path / "pr.json"
    commits.write_text(json.dumps([_github_commit(BOT_EMAIL, message, login="basita-malek[bot]")]))
    pr.write_text(json.dumps({"title": "DFP-1 x", "head": {"ref": "b"}, "body": "", "user": {"login": pr_login}}))
    return ["--commits", str(commits), "--pr", str(pr), "--agents", "malek,salma", "--agent-domain", "example.com"]


def test_cli_maps_bots_from_the_playbooks_identity_config(tmp_path, capsys):
    assert DEFAULT_IDENTITIES.is_file()
    assert json.loads(DEFAULT_IDENTITIES.read_text())["attribution"] == "warn"
    base = _bot_cli(tmp_path)
    assert main([*base, "--format", "github"]) == 0
    assert "::warning title=bot-trailer-missing::" in capsys.readouterr().out
    assert main([*base, "--format", "github", "--bot-mode", "fail"]) == 1
    assert "::error title=bot-trailer-missing::" in capsys.readouterr().out
    assert main([*base, "--bot-mode", "fail", "--warn-only"]) == 0


def test_cli_maps_only_the_listed_agents_bots(tmp_path, capsys):
    base = _bot_cli(tmp_path)
    base[base.index("--agents") + 1] = "salma"
    assert main(base) == 0
    assert "bot-trailer-missing" not in capsys.readouterr().out


def test_cli_reads_an_identities_file(tmp_path, capsys):
    identities = tmp_path / "ids.json"
    identities.write_text(
        json.dumps(
            {
                "attribution": "fail",
                "agents": {
                    "malek": {"kind": "app", "slug": "basita-malek", "bot_id": 1},
                    "yousef": {"kind": "user", "login": "basita-yousef"},
                },
            }
        )
    )
    base = _bot_cli(tmp_path)
    assert main([*base, "--identities", str(identities)]) == 1
    assert "ERROR   [bot-trailer-missing]" in capsys.readouterr().out
    assert main([*base, "--identities", str(identities), "--bot-mode", "warn"]) == 0
    assert "WARNING [bot-trailer-missing]" in capsys.readouterr().out
    identities.write_text(json.dumps({"agents": {}}))
    assert main([*base, "--identities", str(identities)]) == 0
    assert "bot-trailer-missing" not in capsys.readouterr().out


def test_cli_without_an_identity_config_skips_bots(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "DEFAULT_IDENTITIES", tmp_path / "missing.json")
    assert main(_bot_cli(tmp_path)) == 0
    assert "bot-trailer-missing" not in capsys.readouterr().out
