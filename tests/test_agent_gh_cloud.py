"""BASITA-163 (identities M7): App keys in cloud sessions, REST where the proxy blocks GraphQL, the cloud
push gate, gh-cloud-key (setup S1/S8) and the proxy test runner (M0, BASITA-156).

Fakes only: a fake transport for GitHub, a fake `security` and `git`, a fake environment. The one key is
an RSA key the test makes; no real key, token or network is used.
"""

import base64
import copy
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_gh import __main__ as cli
from agent_gh import cloud, cloud_key, proxy_test, rest
from agent_gh.check import run_check
from agent_gh.identity import (
    PR_WRITE,
    READ,
    IdentityError,
    agent_entry,
    app_key,
    app_key_with_source,
    cloud_key_variable,
    env_private_key,
    load_config,
    token_for,
)
from agent_gh.policy import Plan, plan_gh, plan_push

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config" / "github-identities.json")
MINTED = "ghs_fake-installation-value"  # a stand-in, not a real token
OPS = "basitasolutions/basita-ops"
OPS_URL = f"https://github.com/{OPS}.git"
# Assembled at runtime, so no key-looking text is committed (ai-guard's private key rule).
FAKE_PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIfake\n-----END RSA " + "PRIVATE KEY-----"


def ready(*names):
    config = copy.deepcopy(CONFIG)
    for number, name in enumerate(names, start=1):
        entry = config["agents"][name]
        entry.update({"app_id": 9000 + number, "client_id": f"Iv-test-{number}", "bot_id": 8000 + number})
        entry["installations"] = {"basitasolutions": {"id": 7000 + number, "repos": ["basita-ops"]}}
    return config


READY = ready("talia", "sync", "reader", "yousef-docs")


@pytest.fixture(scope="module")
def pem(tmp_path_factory):
    folder = tmp_path_factory.mktemp("key")
    subprocess.run(["openssl", "genrsa", "-out", str(folder / "k.pem"), "2048"], check=True, capture_output=True)
    return (folder / "k.pem").read_bytes()


@pytest.fixture(scope="module")
def other_pem(tmp_path_factory):
    folder = tmp_path_factory.mktemp("key2")
    subprocess.run(["openssl", "genrsa", "-out", str(folder / "k.pem"), "2048"], check=True, capture_output=True)
    return (folder / "k.pem").read_bytes()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def no_security(cmd, **_):
    """A machine without the macOS Keychain, as subprocess.run sees it."""
    raise FileNotFoundError(2, "No such file or directory", cmd[0])


def keychain(values, calls=None):
    def run(cmd, **_):
        if calls is not None:
            calls.append(cmd)
        service = cmd[cmd.index("-s") + 1]
        found = values.get(service)
        return subprocess.CompletedProcess(cmd, 0 if found else 44, stdout=found or "", stderr="")

    return run


class FakeGitHub:
    """Answers REST calls by (method, path), and records (method, path, Authorization, body)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append((method, path, headers.get("Authorization", ""), json.loads(body) if body else None))
        status, payload = self.answers.get((method, path), (404, {"message": "Not Found"}))
        return status, {}, json.dumps(payload) if payload is not None else ""


# --- 1. the key from the environment -----------------------------------------------------------------


def test_the_variable_is_named_after_the_agent():
    assert cloud_key_variable("malek") == "BASITA_GH_MALEK_KEY"
    assert cloud_key_variable("yousef-docs") == "BASITA_GH_YOUSEF_DOCS_KEY"


def test_the_env_key_is_read_only_when_there_is_no_keychain(pem):
    malek = agent_entry(CONFIG, "malek")
    environ = {"BASITA_GH_MALEK_KEY": b64(pem)}
    assert app_key_with_source("malek", malek, no_security, environ) == (pem, "BASITA_GH_MALEK_KEY")
    assert app_key(
        "yousef-docs", agent_entry(READY, "yousef-docs"), no_security, {"BASITA_GH_YOUSEF_DOCS_KEY": b64(pem)}
    )


def test_the_keychain_always_wins_on_the_mac(pem, other_pem):
    malek = agent_entry(CONFIG, "malek")
    environ = {"BASITA_GH_MALEK_KEY": b64(other_pem)}
    assert app_key("malek", malek, keychain({"basita-gh-malek-app": b64(pem)}), environ) == pem
    # A missing Keychain item on the Mac is an error, never a reason to take the variable.
    with pytest.raises(IdentityError, match="no Keychain item basita-gh-malek-app"):
        app_key("malek", malek, keychain({}), environ)


@pytest.mark.parametrize("agent", ["sync", "reader"])
def test_sync_and_reader_never_take_a_cloud_key_even_when_it_is_set(agent, pem):
    environ = {cloud_key_variable(agent): b64(pem)}
    with pytest.raises(IdentityError, match=r"never has a key in the cloud \(plan 10.8\)"):
        app_key(agent, agent_entry(READY, agent), no_security, environ)


def test_any_app_that_updates_main_or_reads_only_is_refused_too(pem):
    for flag in ({"updates_main": "a/b"}, {"read_only": True}):
        entry = {**agent_entry(CONFIG, "malek"), **flag}
        with pytest.raises(IdentityError, match="never has a key in the cloud"):
            env_private_key("malek", entry, {"BASITA_GH_MALEK_KEY": b64(pem)})


@pytest.mark.parametrize(
    "value, why",
    [
        ("not base64!", "does not hold base64 of a private key"),
        (b64(b"hello there"), "does not hold a private key"),
        (FAKE_PEM, "holds a PEM, not one base64"),
    ],
)
def test_a_bad_value_is_refused_and_never_echoed(value, why):
    with pytest.raises(IdentityError, match=why) as error:
        env_private_key("malek", agent_entry(CONFIG, "malek"), {"BASITA_GH_MALEK_KEY": value})
    assert value not in str(error.value) and "MIIfake" not in str(error.value)


def test_a_missing_variable_is_named():
    with pytest.raises(IdentityError, match="no Keychain here and no BASITA_GH_MALEK_KEY"):
        env_private_key("malek", agent_entry(CONFIG, "malek"), {})


def test_yousefs_tokens_stay_keychain_only():
    with pytest.raises(IdentityError, match="kept in the Mac's Keychain only"):
        token_for("yousef", OPS, READ, CONFIG, FakeGitHub({}), no_security)
    with pytest.raises(IdentityError, match="kept in the Mac's Keychain only"):
        env_private_key("yousef", agent_entry(CONFIG, "yousef"), {"BASITA_GH_YOUSEF_KEY": "x"})


def test_a_token_is_minted_in_the_cloud_with_the_env_key(pem, monkeypatch):
    monkeypatch.setenv("BASITA_GH_MALEK_KEY", b64(pem))
    github = FakeGitHub({("POST", "/app/installations/177981619/access_tokens"): (201, {"token": MINTED})})
    assert token_for("malek", OPS, READ, CONFIG, github, no_security) == (MINTED, True)
    assert github.calls[0][2].startswith("Bearer ")


def test_gh_check_in_the_cloud_says_where_the_key_came_from(pem, monkeypatch):
    monkeypatch.setenv("BASITA_GH_MALEK_KEY", b64(pem))
    github = FakeGitHub({("GET", "/app"): (200, {"slug": "basita-malek", "id": 5586450, "permissions": {}})})
    lines, _ = run_check(CONFIG, ["malek"], github, no_security)
    assert "  ok key: BASITA_GH_MALEK_KEY holds a private key" in lines
    assert b64(pem) not in "\n".join(lines)


# --- detection and the cloud push gate ----------------------------------------------------------------


@pytest.mark.parametrize(
    "environ, cloud_on, rest_on",
    [
        ({}, False, False),
        ({"CLAUDE_CODE_REMOTE": "true"}, True, True),
        ({"CLAUDE_CODE_REMOTE": "false"}, False, False),
        ({"AGENT_GH_REST": "1"}, False, True),
        ({"CLAUDE_CODE_REMOTE": "true", "AGENT_GH_REST": "0"}, True, False),
    ],
)
def test_cloud_and_rest_mode_detection(environ, cloud_on, rest_on):
    assert cloud.in_cloud(environ) is cloud_on
    assert cloud.rest_mode(environ) is rest_on


def test_a_key_from_the_environment_means_a_cloud_session_even_without_claude_code_remote(tmp_path):
    """Yousef's review of #37: the gates don't rest on CLAUDE_CODE_REMOTE alone."""
    no_keychain = {"PATH": str(tmp_path), "BASITA_GH_MALEK_KEY": "x"}  # a PATH with no `security`
    assert cloud.key_from_environment(no_keychain) and cloud.in_cloud(no_keychain)
    with pytest.raises(IdentityError, match="waits for the proxy test"):
        cloud.require_cloud_push(no_keychain)
    with pytest.raises(IdentityError, match="waits for the proxy test"):
        cloud.require_cloud_write(PR_WRITE, no_keychain)
    assert cloud.push_env("t", no_keychain)  # the token goes as the header, as in any cloud session
    # Not the cloud: no key variable (or an empty one), or a Keychain, which identity.py prefers.
    assert not cloud.in_cloud({"PATH": str(tmp_path)})
    assert not cloud.in_cloud({"PATH": str(tmp_path), "BASITA_GH_MALEK_KEY": " "})
    (tmp_path / "security").write_text("#!/bin/sh\n")
    (tmp_path / "security").chmod(0o755)
    assert not cloud.in_cloud(no_keychain)
    assert not cloud.in_cloud({"PATH": str(tmp_path), "BASITA_GH_KEY_NOTE": "x"})


def test_a_cloud_push_waits_for_m0():
    cloud.require_cloud_push({})  # the Mac: nothing changes
    with pytest.raises(IdentityError, match=r"waits for the proxy test \(M0, BASITA-156\)"):
        cloud.require_cloud_push({"CLAUDE_CODE_REMOTE": "true"})
    with pytest.raises(IdentityError, match="Git Data API is not built yet"):
        cloud.require_cloud_push({"CLAUDE_CODE_REMOTE": "true", "AGENT_GIT_CLOUD_PUSH": "git-data"})
    cloud.require_cloud_push({"CLAUDE_CODE_REMOTE": "true", "AGENT_GIT_CLOUD_PUSH": "git"})


def test_a_cloud_write_waits_for_m0_and_a_read_does_not():
    on_cloud = {"CLAUDE_CODE_REMOTE": "true"}
    cloud.require_cloud_write(PR_WRITE, {})  # the Mac
    cloud.require_cloud_write(READ, on_cloud)
    with pytest.raises(IdentityError, match=r"write from a cloud session waits for the proxy test"):
        cloud.require_cloud_write(PR_WRITE, on_cloud)
    cloud.require_cloud_write(PR_WRITE, {**on_cloud, "AGENT_GH_CLOUD_WRITES": "1"})


def test_cmd_gh_refuses_a_cloud_write_before_a_token_is_minted(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.delenv("AGENT_GH_CLOUD_WRITES", raising=False)
    monkeypatch.setattr(cli, "_default_repo", lambda: OPS)
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: pytest.fail("a token was minted"))
    with pytest.raises(IdentityError, match="waits for the proxy test"):
        cli.cmd_gh("malek", ["pr", "comment", "3", "--body", "x"])
    with pytest.raises(IdentityError, match="waits for the proxy test"):
        cli.cmd_gh("malek", ["api", f"repos/{OPS}/issues/3/comments", "-f", "body=x"])


def test_the_cloud_push_token_goes_as_a_header_in_the_environment():
    assert cloud.push_env(MINTED, {}) == {}
    env = cloud.push_env(MINTED, {"CLAUDE_CODE_REMOTE": "true"})
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    scheme, value = env["GIT_CONFIG_VALUE_0"].split(": ", 1)[1].split(" ")
    assert scheme == "Basic" and base64.b64decode(value).decode() == f"x-access-token:{MINTED}"


def _clone(tmp_path, branch):
    repo = tmp_path / "repo"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", OPS_URL], check=True, env=env)
    return repo


def test_cmd_push_in_the_cloud_is_refused_before_a_token_is_minted(tmp_path, monkeypatch):
    monkeypatch.chdir(_clone(tmp_path, "malek/BASITA-163-x"))
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.delenv("AGENT_GIT_CLOUD_PUSH", raising=False)
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: pytest.fail("a token was minted"))
    with pytest.raises(IdentityError, match="waits for the proxy test"):
        cli.cmd_push("malek", [])


def test_cmd_push_in_the_cloud_once_m0_passed_keeps_the_token_out_of_argv(tmp_path, monkeypatch):
    calls = []
    monkeypatch.chdir(_clone(tmp_path, "malek/BASITA-163-x"))
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("AGENT_GIT_CLOUD_PUSH", "git")
    monkeypatch.delenv("AGENT_GH_DRY_RUN", raising=False)
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: (MINTED, True))
    monkeypatch.setattr(cli, "run_with_token", lambda cmd, env, token, revocable: calls.append((cmd, env)) or 0)
    assert cli.cmd_push("malek", []) == 0
    command, env = calls[0]
    assert MINTED not in " ".join(command) and command[-2:] == ["origin", "HEAD:refs/heads/malek/BASITA-163-x"]
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"


def test_the_branch_prefix_policy_is_unchanged_in_the_cloud(tmp_path, monkeypatch):
    monkeypatch.chdir(_clone(tmp_path, "feature/x"))
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("AGENT_GIT_CLOUD_PUSH", "git")
    with pytest.raises(IdentityError, match=r"pushes only to malek/\* branches"):
        cli.cmd_push("malek", [])


# --- 2. REST where the proxy blocks GraphQL -----------------------------------------------------------


def plan(args, agent="malek", config=CONFIG):
    return plan_gh(agent, agent_entry(config, agent), args, OPS, config)


def run_rest(args, answers, branch="malek/BASITA-163-x", stdin="", agent="malek"):
    github = FakeGitHub(answers)
    out = io.StringIO()
    runner = rest.runner(agent, plan(args, agent))
    code = runner(MINTED, github, branch=lambda: branch, out=out, stdin=io.StringIO(stdin))
    return code, out.getvalue(), github


@pytest.mark.parametrize(
    "args",
    [["pr", "edit", "3", "--title", "x"], ["pr", "list"], ["issue", "view", "3"], ["pr", "review", "3", "-c"]],
)
def test_commands_with_no_rest_mapping_are_refused_with_the_way_round(args):
    with pytest.raises(IdentityError, match=r"GraphQL is blocked in the cloud.*use `agent-gh malek api …`"):
        rest.runner("malek", plan(args))


def test_gh_api_runs_gh_itself_unless_it_is_graphql():
    assert rest.runner("malek", plan(["api", f"repos/{OPS}/pulls/3"])) is None
    with pytest.raises(IdentityError, match="GraphQL is blocked in the cloud"):
        rest.runner("malek", plan(["api", "graphql", "-f", "query={viewer{login}}"]))


def test_the_policy_still_judges_the_original_command(monkeypatch):
    monkeypatch.setenv("AGENT_GH_REST", "1")
    monkeypatch.setattr(cli, "_default_repo", lambda: OPS)
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: pytest.fail("a token was minted"))
    with pytest.raises(IdentityError, match="merging is Ahmed's"):
        cli.cmd_gh("malek", ["pr", "merge", "3"])
    with pytest.raises(IdentityError, match="set only by Ahmed"):
        cli.cmd_gh("malek", ["pr", "create", "--title", "x", "--label", "reviewed-by-ahmed"])
    with pytest.raises(IdentityError, match="GraphQL is blocked"):
        cli.cmd_gh("malek", ["pr", "edit", "3", "--title", "x"])


def test_pr_create_through_rest(tmp_path):
    body = tmp_path / "pr.md"
    body.write_text("## What\nx\n")
    answers = {
        ("GET", f"/repos/{OPS}"): (200, {"default_branch": "main"}),
        ("POST", f"/repos/{OPS}/pulls"): (201, {"number": 7, "html_url": "https://github.com/x/pull/7"}),
        ("POST", f"/repos/{OPS}/issues/7/labels"): (200, []),
        ("POST", f"/repos/{OPS}/pulls/7/requested_reviewers"): (201, {}),
    }
    args = ["pr", "create", "--title=BASITA-163: x", "-F", str(body), "--draft", "-l", "a,b", "-r", "basita-yousef"]
    code, out, github = run_rest(args, answers)
    assert code == 0 and out == "https://github.com/x/pull/7\n"
    calls = {(m, p): b for m, p, _, b in github.calls}
    assert calls[("POST", f"/repos/{OPS}/pulls")] == {
        "title": "BASITA-163: x",
        "head": "malek/BASITA-163-x",
        "base": "main",
        "body": "## What\nx\n",
        "draft": True,
    }
    assert calls[("POST", f"/repos/{OPS}/issues/7/labels")] == {"labels": ["a", "b"]}
    assert calls[("POST", f"/repos/{OPS}/pulls/7/requested_reviewers")] == {
        "reviewers": ["basita-yousef"],
        "team_reviewers": [],
    }
    assert all(auth == f"token {MINTED}" for _, _, auth, _ in github.calls)


def test_pr_create_with_base_head_and_body_from_stdin():
    answers = {("POST", f"/repos/{OPS}/pulls"): (201, {"number": 8})}
    code, out, github = run_rest(
        ["pr", "create", "-t", "x", "-B", "dev", "-H", "malek/y", "--body-file", "-"], answers, stdin="hi"
    )
    assert code == 0 and out == f"https://github.com/{OPS}/pull/8\n"
    assert github.calls[0][3] == {"title": "x", "head": "malek/y", "base": "dev", "body": "hi"}


@pytest.mark.parametrize(
    "args, why",
    [
        (["pr", "create", "--fill"], "`gh pr create --fill`"),
        (["pr", "create", "--title", "x", "--web"], "`gh pr create --web`"),
        (["pr", "create", "--body", "x"], "--title is needed"),
        (["pr", "create", "-t", "x", "-b", "y", "-F", "z"], "not both"),
        (["pr", "comment", "3", "--edit-last", "-b", "x"], "`gh pr comment --edit-last`"),
        (["pr", "comment", "3"], "--body or --body-file is needed"),
        (["pr", "view", "3", "--comments"], "`gh pr view --comments`"),
        (["pr", "view", "3", "--json", "reviewDecision"], "no REST mapping"),
        (["pr", "checks", "3", "--watch"], "`gh pr checks --watch`"),
        (["pr", "view", "https://github.com/other/repo/pull/3", "-R", OPS], "more than one repository"),
    ],
)
def test_rest_refusals(args, why):
    with pytest.raises(IdentityError, match=why):
        run_rest(args, {})


PR = {
    "number": 3,
    "title": "BASITA-163: x",
    "state": "open",
    "draft": False,
    "html_url": "https://github.com/basitasolutions/basita-ops/pull/3",
    "user": {"login": "basita-malek[bot]"},
    "head": {"ref": "malek/BASITA-163-x", "sha": "abc123"},
    "base": {"ref": "main"},
    "labels": [{"name": "run-e2e"}],
    "mergeable": True,
    "body": "the body",
    "additions": 5,
    "deletions": 1,
}


def test_pr_view_text_and_json():
    answers = {("GET", f"/repos/{OPS}/pulls/3"): (200, PR)}
    code, out, _ = run_rest(["pr", "view", "3"], answers)
    assert code == 0
    assert "title:\tBASITA-163: x\nstate:\tOPEN\nauthor:\tbasita-malek[bot]\nlabels:\trun-e2e\nnumber:\t3\n" in out
    assert out.endswith("--\nthe body\n")
    code, out, _ = run_rest(
        ["pr", "view", "#3", "--json", "number,state,headRefName,isDraft,mergeable,labels"], answers
    )
    assert json.loads(out) == {
        "number": 3,
        "state": "OPEN",
        "headRefName": "malek/BASITA-163-x",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "labels": [{"name": "run-e2e"}],
    }
    merged = {**PR, "state": "closed", "merged_at": "2026-11-03T10:00:00Z", "mergeable": None}
    code, out, _ = run_rest(
        ["pr", "view", PR["html_url"], "--json=state,mergeable"], {("GET", f"/repos/{OPS}/pulls/3"): (200, merged)}
    )
    assert json.loads(out) == {"state": "MERGED", "mergeable": "UNKNOWN"}


def test_pr_view_finds_the_pull_request_of_the_current_branch():
    answers = {
        ("GET", f"/repos/{OPS}/pulls?head=basitasolutions:malek/BASITA-163-x&state=all&per_page=1"): (200, [PR]),
        ("GET", f"/repos/{OPS}/pulls/3"): (200, PR),
    }
    code, out, _ = run_rest(["pr", "view"], answers)
    assert code == 0 and "number:\t3" in out
    with pytest.raises(IdentityError, match="no pull request found for the branch other"):
        run_rest(
            ["pr", "view", "other"],
            {("GET", f"/repos/{OPS}/pulls?head=basitasolutions:other&state=all&per_page=1"): (200, [])},
        )


def test_pr_comment_through_rest():
    answers = {("POST", f"/repos/{OPS}/issues/3/comments"): (201, {"html_url": "https://github.com/c/1"})}
    code, out, github = run_rest(["pr", "comment", "3", "--body", "Rework done."], answers)
    assert code == 0 and out == "https://github.com/c/1\n"
    assert github.calls[0][3] == {"body": "Rework done."}


def _checks(runs, statuses, status=200):
    return {
        ("GET", f"/repos/{OPS}/pulls/3"): (200, PR),
        ("GET", f"/repos/{OPS}/commits/abc123/check-runs?per_page=100&page=1"): (status, {"check_runs": runs}),
        ("GET", f"/repos/{OPS}/commits/abc123/status"): (200, {"statuses": statuses}),
    }


def run_(name, status="completed", conclusion="success", number=1):
    return {
        "id": number,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://x/{name}",
        "started_at": "2026-11-03T10:00:00Z",
        "completed_at": "2026-11-03T10:01:05Z",
    }


def test_pr_checks_buckets_and_exit_codes():
    code, out, _ = run_rest(["pr", "checks", "3"], _checks([run_("ci"), run_("ai-guard", conclusion="skipped")], []))
    assert code == 0 and "ci\tpass\t1m5s\thttps://x/ci\t" in out and "ai-guard\tskipping" in out
    code, out, _ = run_rest(["pr", "checks", "3"], _checks([run_("ci", status="in_progress")], []))
    assert code == 8 and "ci\tpending" in out
    statuses = [{"context": "Jira status", "state": "failure", "target_url": "https://j", "description": "not ready"}]
    code, out, _ = run_rest(["pr", "checks", "3"], _checks([run_("ci")], statuses))
    assert code == 1 and out.splitlines()[0] == "Jira status\tfail\t0\thttps://j\tnot ready"


def test_pr_checks_keeps_the_latest_run_of_each_name():
    runs = [run_("ci", conclusion="failure", number=1), run_("ci", number=2)]
    code, out, _ = run_rest(["pr", "checks", "3"], _checks(runs, []))
    assert code == 0 and len(out.splitlines()) == 1


def test_pr_checks_without_checks_or_permission(capsys):
    code, _, _ = run_rest(["pr", "checks", "3"], _checks([], []))
    assert code == 1 and "no checks reported" in capsys.readouterr().err
    with pytest.raises(IdentityError, match="can't read checks or commit statuses here"):
        run_rest(["pr", "checks", "3"], _checks([], [], status=403))


def test_cmd_gh_in_rest_mode_mints_runs_and_revokes(monkeypatch, capsys):
    github = FakeGitHub({("GET", f"/repos/{OPS}/pulls/3"): (200, PR), ("DELETE", "/installation/token"): (204, None)})
    original = rest.run_and_revoke
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.delenv("AGENT_GH_REST", raising=False)
    monkeypatch.delenv("AGENT_GH_DRY_RUN", raising=False)
    monkeypatch.setattr(cli, "_default_repo", lambda: OPS)
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: (MINTED, True))
    monkeypatch.setattr(cli, "run_with_token", lambda *a, **k: pytest.fail("gh ran"))
    monkeypatch.setattr(rest, "run_and_revoke", lambda run, token, revocable: original(run, token, revocable, github))
    assert cli.cmd_gh("malek", ["pr", "view", "3", "--json", "number"]) == 0
    assert capsys.readouterr().out == '{"number":3}\n'
    assert github.calls[-1][:3] == ("DELETE", "/installation/token", f"token {MINTED}")


def test_a_failed_rest_command_still_revokes():
    github = FakeGitHub({("DELETE", "/installation/token"): (204, None)})
    runner = rest.runner("malek", plan(["pr", "view", "3"]))
    with pytest.raises(IdentityError, match="404"):
        rest.run_and_revoke(runner, MINTED, True, github)
    assert github.calls[-1][0] == "DELETE"


def test_dry_run_says_rest(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_GH_REST", "1")
    monkeypatch.setenv("AGENT_GH_DRY_RUN", "1")
    monkeypatch.setattr(cli, "_default_repo", lambda: OPS)
    assert cli.cmd_gh("malek", ["pr", "checks", "3"]) == 0
    assert "malek gh (REST) on basitasolutions/basita-ops" in capsys.readouterr().out


# --- 4. gh-cloud-key ----------------------------------------------------------------------------------


def _app(slug="basita-malek", number=5586450):
    return FakeGitHub({("GET", "/app"): (200, {"slug": slug, "id": number})})


def test_gh_cloud_key_copies_one_line_and_prints_only_its_length(pem, other_pem, tmp_path, monkeypatch, capsys):
    file = tmp_path / "basita-malek.private-key.pem"
    file.write_bytes(pem)
    monkeypatch.chdir(tmp_path)
    copied, commands = [], []
    message = cloud_key.cloud_key(
        "malek",
        str(file),
        CONFIG,
        _app(),
        copied.append,
        keychain({"basita-gh-malek-app": b64(other_pem)}, commands),
    )
    line = b64(pem.strip() + b"\n")
    assert copied == [line] and "\n" not in line
    assert message == (
        f"copied the cloud key of basita-malek ({len(line)} chars); paste it into the claude.ai environment as "
        "BASITA_GH_MALEK_KEY, then clear the clipboard (pbcopy </dev/null) and move the .pem to the Trash"
    )
    assert line not in message and b"PRIVATE" not in message.encode()
    assert all(c[:2] == ["security", "find-generic-password"] for c in commands)  # nothing stored
    assert sorted(p.name for p in tmp_path.iterdir()) == ["basita-malek.private-key.pem"]  # nothing written
    assert capsys.readouterr().out == ""


def test_gh_cloud_key_refuses_the_wrong_app_the_macs_key_and_a_non_key(pem, tmp_path):
    file = tmp_path / "k.pem"
    file.write_bytes(pem)
    copied = []
    with pytest.raises(IdentityError, match="belongs to basita-haya.*nothing copied"):
        cloud_key.cloud_key("malek", str(file), CONFIG, _app("basita-haya", 5586509), copied.append, keychain({}))
    with pytest.raises(IdentityError, match="this is the Mac's key"):
        cloud_key.cloud_key(
            "malek", str(file), CONFIG, _app(), copied.append, keychain({"basita-gh-malek-app": b64(pem)})
        )
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(IdentityError, match="is not a private key"):
        cloud_key.cloud_key("malek", str(tmp_path / "notes.txt"), CONFIG, _app(), copied.append, keychain({}))
    assert copied == []


@pytest.mark.parametrize(
    "agent, config, why",
    [
        ("sync", READY, "never has a key in the cloud"),
        ("reader", READY, "never has a key in the cloud"),
        ("yousef", CONFIG, "is a user"),
        ("talia", CONFIG, "not set up yet"),
    ],
)
def test_gh_cloud_key_refusals_before_the_file_is_read(agent, config, why, tmp_path):
    github = _app()
    with pytest.raises(IdentityError, match=why):
        cloud_key.cloud_key(agent, str(tmp_path / "missing.pem"), config, github, pytest.fail, keychain({}))
    assert github.calls == []


def test_gh_cloud_key_usage(capsys):
    assert cloud_key.main([]) == 2
    assert cloud_key.main(["malek", "--file", "/nonexistent/k.pem"]) == 1
    assert "cannot read" in capsys.readouterr().err


# --- 5. the proxy test (M0) ---------------------------------------------------------------------------

BOT = "basita-malek[bot]"
SHA = "c0ffee" * 6 + "abcd"
TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class FakeGit:
    """git and gh as subprocess.run sees them: records each command and its environment."""

    def __init__(self, push_code=0, push_err="", gh_out="HTTP/2.0 200 OK\n", rewrites=""):
        self.calls = []
        self.push_code, self.push_err, self.gh_out, self.rewrites = push_code, push_err, gh_out, rewrites

    def __call__(self, command, env=None, input=None, **_):
        self.calls.append((command, env or {}, input))
        done = subprocess.CompletedProcess
        if command[0] == "gh":
            return done(command, 0, stdout=self.gh_out, stderr="")
        if "hash-object" in command:
            return done(command, 0, stdout=TREE + "\n", stderr="")
        if "commit-tree" in command:
            return done(command, 0, stdout=SHA + "\n", stderr="")
        if "rev-parse" in command:
            return done(command, 0, stdout="feed" * 10 + "\n", stderr="")
        if "--get-regexp" in command:
            return done(command, 0 if self.rewrites else 1, stdout=self.rewrites, stderr="")
        if "push" in command:
            return done(command, self.push_code, stdout="", stderr=self.push_err)
        return done(command, 0, stdout="", stderr="")


def _answers(pusher=BOT, installation_status=200, user_with_token=(403, {}), graphql=(403, {"message": "blocked"})):
    return {
        ("GET", "/user"): (200, {"login": "ahmedoraby-eng"}),
        ("GET", "/app"): (200, {"slug": "basita-malek", "id": 5586450}),
        ("POST", "/app/installations/177981619/access_tokens"): (201, {"token": MINTED}),
        ("GET", "/installation/repositories?per_page=1"): (installation_status, {"total_count": 2}),
        ("GET", f"/repos/{OPS}"): (200, {"full_name": OPS}),
        ("POST", "/graphql"): graphql,
        ("GET", f"/repos/{OPS}/activity?ref=refs/heads/malek/BASITA-153-proxy-test&per_page=1"): (
            200,
            [{"after": SHA, "actor": {"login": pusher}}],
        ),
        ("GET", f"/repos/{OPS}/commits/{SHA}"): (200, {"author": {"login": BOT}}),
        ("DELETE", "/installation/token"): (204, None),
    }


class UserByAuth(FakeGitHub):
    """GET /user answers by whether the request carried a token, as the proxy would."""

    def __init__(self, answers, with_token):
        super().__init__(answers)
        self.with_token = with_token

    def __call__(self, method, url, headers, body):
        if method == "GET" and url.endswith("/user") and headers.get("Authorization"):
            self.calls.append((method, "/user", headers["Authorization"], None))
            status, payload = self.with_token
            return status, {}, json.dumps(payload)
        return super().__call__(method, url, headers, body)


def run_proxy(pem, answers, git=None, with_token=(403, {}), agent="malek", config=CONFIG, environ=None, capsys=None):
    github = UserByAuth(answers, with_token)
    git = git or FakeGit()
    environ = {"CLAUDE_CODE_REMOTE": "true", "BASITA_GH_MALEK_KEY": b64(pem), "PATH": os.environ["PATH"]}
    test = proxy_test.ProxyTest(
        agent, OPS, config, github, git, environ, sleep=lambda _: None, workdir=lambda: "/tmp/m0-fake"
    )
    code = test.main()
    return code, test.lines, github, git


def test_the_proxy_test_when_the_apps_token_passes_through(pem, capsys):
    code, lines, github, git = run_proxy(pem, _answers())
    assert code == 0
    text = "\n".join(lines)
    assert lines[0] == "a. no Authorization: GET /user -> 200, login ahmedoraby-eng"
    assert "slug basita-malek; the JWT passed through: yes" in lines[1]
    assert lines[2].endswith("(contents write, metadata read): ok")
    assert "GET /installation/repositories -> 200 (the App's token passed through: yes); GET /user -> 403" in lines[3]
    assert lines[4] == "e. gh api with GH_TOKEN: installation/repositories -> 200"
    assert lines[5] == "f. GraphQL with the token: POST /graphql -> 403"
    assert "to malek/BASITA-153-proxy-test (git rewrites github.com: no): pushed; pushed by basita-malek[bot]" in text
    assert "branch left for Malek to report; Ahmed may delete it" in text
    assert lines[-3] == "h. token revoked: yes (204)"
    assert lines[-2] == "RESULT: push=app rest=app graphql=blocked"
    assert lines[-1].startswith("HUMAN:") and "/web-setup" in lines[-1]
    # Nothing secret anywhere: not in the output, not in any command's arguments.
    assert MINTED not in text and b64(pem) not in text and MINTED not in capsys.readouterr().out
    assert all(MINTED not in " ".join(command) for command, _, _ in git.calls)
    push = next((c, e) for c, e, _ in git.calls if "push" in c)
    assert push[0][-1] == f"{SHA}:refs/heads/malek/BASITA-153-proxy-test" and push[0][-2] == OPS_URL
    assert base64.b64decode(push[1]["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == f"x-access-token:{MINTED}"
    assert push[1]["GIT_AUTHOR_EMAIL"] == "351705446+basita-malek[bot]@users.noreply.github.com"
    gh_env = next(e for c, e, _ in git.calls if c[0] == "gh")
    assert gh_env["GH_TOKEN"] == MINTED
    # Never deletes: the only DELETE is the token's own revocation.
    assert [p for m, p, _, _ in github.calls if m == "DELETE"] == ["/installation/token"]


def test_the_proxy_test_when_the_proxy_puts_ahmeds_token_in_its_place(pem):
    answers = _answers(
        pusher="ahmedoraby-eng",
        installation_status=403,
        graphql=(200, {"data": {"viewer": {"login": "ahmedoraby-eng"}}}),
    )
    code, lines, _, _ = run_proxy(pem, answers, with_token=(200, {"login": "ahmedoraby-eng"}))
    assert code == 0
    assert "GET /user -> 200, login ahmedoraby-eng" in lines[3]
    assert lines[-2] == "RESULT: push=ahmed rest=ahmed graphql=ahmed"


def test_the_proxy_test_reports_a_refused_push_with_the_login_only(pem):
    git = FakeGit(
        push_code=128, push_err="remote: Permission to basitasolutions/basita-ops.git denied to basita-yousef.\n"
    )
    code, lines, _, _ = run_proxy(pem, _answers(), git)
    assert code == 0
    assert any(line.endswith("refused (exit 128), denied to basita-yousef") for line in lines)
    assert lines[-2] == "RESULT: push=refused rest=app graphql=blocked"


def test_the_proxy_test_builds_on_an_earlier_test_commit_and_sees_a_rewrite(pem):
    answers = _answers()
    answers[("GET", f"/repos/{OPS}/git/ref/heads/malek/BASITA-153-proxy-test")] = (200, {"object": {"sha": "x"}})
    git = FakeGit(rewrites="url.http://127.0.0.1:9/git/.insteadof https://github.com/\n")
    _, lines, _, git = run_proxy(pem, answers, git)
    commands = [c for c, _, _ in git.calls]
    assert any("fetch" in c and "--depth=1" in c for c in commands)
    commit = next(c for c in commands if "commit-tree" in c)
    assert commit[commit.index("-p") + 1] == "feed" * 10
    assert any("(git rewrites github.com: yes)" in line for line in lines)


def test_the_proxy_test_revokes_even_when_a_step_breaks(pem):
    class Broken(FakeGit):
        def __call__(self, command, **kwargs):
            if command[0] == "gh":
                raise RuntimeError("boom")
            return super().__call__(command, **kwargs)

    github = UserByAuth(_answers(), (403, {}))
    environ = {"CLAUDE_CODE_REMOTE": "true", "BASITA_GH_MALEK_KEY": b64(pem)}
    test = proxy_test.ProxyTest("malek", OPS, CONFIG, github, Broken(), environ, workdir=lambda: "/tmp/m0")
    with pytest.raises(RuntimeError):
        test.main()
    assert github.calls[-1][:2] == ("DELETE", "/installation/token")


@pytest.mark.parametrize("agent", ["sync", "reader"])
def test_the_proxy_test_refuses_sync_and_reader_before_any_key_call(agent, pem):
    github = UserByAuth({("GET", "/user"): (200, {"login": "ahmedoraby-eng"})}, (403, {}))
    environ = {cloud_key_variable(agent): b64(pem)}
    test = proxy_test.ProxyTest(agent, OPS, READY, github, FakeGit(), environ)
    assert test.main() == 1
    assert "never has a key in the cloud" in test.lines[2]
    assert [p for _, p, _, _ in github.calls] == ["/user"]


def test_the_proxy_test_without_a_key_or_with_a_repo_outside_the_installations(pem):
    github = UserByAuth({}, (403, {}))
    test = proxy_test.ProxyTest("malek", OPS, CONFIG, github, FakeGit(), {})
    assert test.main() == 1 and "no BASITA_GH_MALEK_KEY" in test.lines[-3]
    assert "not a cloud session" in test.lines[0]
    test = proxy_test.ProxyTest("malek", "someone/repo", CONFIG, github, FakeGit(), {"BASITA_GH_MALEK_KEY": b64(pem)})
    assert test.main() == 1 and "no listed installation for someone" in test.lines[-3]


def test_the_proxy_test_command_line(monkeypatch, capsys):
    assert proxy_test.main([]) == 2
    assert proxy_test.main(["maya", OPS]) == 1
    monkeypatch.setattr(proxy_test, "urllib_transport", UserByAuth({("GET", "/user"): (401, {})}, (403, {})))
    assert proxy_test.main(["--whoami"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("a. no Authorization: GET /user -> 401\nHUMAN:")


def test_the_scripts_are_executable_and_say_their_usage():
    for name in ("gh-cloud-key", "gh-proxy-test"):
        script = ROOT / "scripts" / name
        assert os.access(script, os.X_OK)
        result = subprocess.run([str(script)], capture_output=True, text=True)
        assert result.returncode == 2 and "usage:" in result.stderr


def test_rest_plan_is_the_policys_plan():
    """REST mode runs the very Plan the policy made: same repository and permissions."""
    planned = plan(["pr", "comment", "3", "--body", "x"])
    assert isinstance(planned, Plan) and planned.permissions["pull_requests"] == "write"
    assert plan_push(["origin"], "malek/x", OPS_URL, CONFIG, agent_entry(CONFIG, "malek")).repo == OPS
