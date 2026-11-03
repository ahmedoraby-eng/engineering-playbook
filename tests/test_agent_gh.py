import base64
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_gh import __main__ as cli
from agent_gh.check import run_check
from agent_gh.identity import (
    PR_WRITE,
    PUSH,
    READ,
    IdentityError,
    agent_entry,
    app_jwt,
    bot_email,
    load_config,
    private_key,
    token_for,
)
from agent_gh.policy import plan_gh, plan_push, repo_from_remote

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config" / "github-identities.json")
MINTED = "ghs_fake-installation-value"  # a stand-in, not a real token


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    """A throwaway RSA key made for the test, and its public half to verify signatures."""
    folder = tmp_path_factory.mktemp("key")
    subprocess.run(["openssl", "genrsa", "-out", str(folder / "k.pem"), "2048"], check=True, capture_output=True)
    subprocess.run(
        ["openssl", "rsa", "-in", str(folder / "k.pem"), "-pubout", "-out", str(folder / "pub.pem")],
        check=True,
        capture_output=True,
    )
    return (folder / "k.pem").read_bytes(), folder / "pub.pem"


def keychain(values):
    """A stand-in for `security find-generic-password`: service -> value."""

    def run(cmd, **_):
        service = cmd[cmd.index("-s") + 1]
        found = values.get(service)
        return subprocess.CompletedProcess(cmd, 0 if found else 44, stdout=found or "", stderr="")

    return run


class FakeGitHub:
    """Answers the REST calls by (method, path), and records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append((method, path, headers.get("Authorization", ""), json.loads(body) if body else None))
        status, response_headers, payload = self.answers.get((method, path), (404, {}, {"message": "Not Found"}))
        return status, response_headers, json.dumps(payload)


def plan(agent, *args, repo="basitasolutions/basita-ops"):
    return plan_gh(agent, agent_entry(CONFIG, agent), list(args), repo, CONFIG)


# --- policy: gh -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, permissions",
    [
        (["pr", "view", "12"], READ),
        (["pr", "list", "--state", "open"], READ),
        (["pr", "create", "-d", "-t", "DFP-1: x", "-b", "body"], PR_WRITE),
        (["pr", "edit", "3", "--add-label", "run-e2e"], PR_WRITE),
        (["pr", "comment", "3", "--body", "hi"], PR_WRITE),
        (["issue", "comment", "3", "--body", "hi"], PR_WRITE),
        (["api", "repos/{owner}/{repo}/pulls/3"], READ),
        (["api", "repos/{owner}/{repo}/issues/3/comments", "-f", "body=hi"], PR_WRITE),
        (["api", "repos/basitasolutions/basita-ops/pulls/3/requested_reviewers", "-f", "reviewers[]=x"], PR_WRITE),
        (["api", "graphql", "-f", "query={ viewer { login } }"], READ),
    ],
)
def test_allowed_gh_commands_get_the_smallest_token(args, permissions):
    assert plan("malek", *args).permissions == permissions


@pytest.mark.parametrize(
    "args, why",
    [
        (["pr", "merge", "3"], "merging"),
        (["pr", "close", "3"], "closing"),
        (["auth", "status"], "login"),
        (["secret", "set", "X"], "secrets"),
        (["workflow", "run", "ci"], "workflows"),
        (["repo", "edit", "--visibility", "public"], "not on agent-gh's list"),
        (["repo", "delete", "x/y"], "not on agent-gh's list"),
        (["label", "create", "x"], "not on agent-gh's list"),
        (["pr", "edit", "3", "--add-label", "reviewed-by-ahmed"], "only by Ahmed"),
        (["pr", "create", "--label=docs,Human-Reviewed"], "only by Ahmed"),
        (["pr", "edit", "3", "-lreviewed-by-ahmed"], "only by Ahmed"),
        (["pr", "edit", "3", "--add-label", "jira-status-waived"], "only by Ahmed"),
        (["api", "repos/a/b/issues/3/labels", "-f", "labels[]=Jira-Status-Waived"], "only by Ahmed"),
        (["pr", "review", "3", "--approve"], "Yousef reviews"),
        (["pr", "view", "3", "--hostname", "evil.example"], "github.com"),
        (["api", "-X", "DELETE", "repos/a/b/git/refs/heads/x"], "deleting"),
        (["api", "--method=PUT", "repos/a/b/pulls/3/merge"], "not on agent-gh's list"),
        (["api", "-XPATCH", "repos/a/b"], "not on agent-gh's list"),
        (["api", "repos/a/b/git/refs", "-f", "ref=refs/heads/main", "-f", "sha=1"], "not on agent-gh's list"),
        (["api", "repos/a/b/contents/x", "-X", "PUT", "-f", "content=eA=="], "not on agent-gh's list"),
        (["api", "repos/a/b/pulls/3", "-X", "PATCH", "-f", "state=closed"], "closing"),
        (["api", "repos/a/b/issues/3/labels", "-f", "labels[]=reviewed-by-ahmed"], "only by Ahmed"),
        (["api", "repos/a/b/issues/3/comments", "--input", "body.json"], "--input"),
        (["api", "repos/a/b/pulls/3/reviews", "-f", "event=APPROVE"], "Yousef reviews"),
        (["api", "graphql", "-f", "query=mutation { mergePullRequest }"], "mutations"),
        (["api", "user", "-X", "PATCH", "-f", "name=x"], "outside a repository"),
        (["pr", "view", "-R", "a/b", "https://github.com/c/d/pull/1"], "more than one repository"),
    ],
)
def test_refusals(args, why):
    with pytest.raises(IdentityError, match=why) as error:
        plan("malek", *args)
    assert str(error.value).startswith("refused") or "repository" in str(error.value)


def test_yousef_may_approve_and_the_repo_comes_from_the_flag_or_url():
    assert plan("yousef", "pr", "review", "3", "--approve", "-b", "ok").permissions == PR_WRITE
    assert plan("malek", "pr", "view", "1", "-R", "ahmedoraby-eng/engineering-playbook").repo == (
        "ahmedoraby-eng/engineering-playbook"
    )
    assert plan("malek", "pr", "view", "https://github.com/basitasolutions/x/pull/3", repo=None).repo == (
        "basitasolutions/x"
    )
    assert plan("malek", "api", "repos/{owner}/{repo}/pulls").repo == "basitasolutions/basita-ops"
    with pytest.raises(IdentityError, match="no repository"):
        plan("malek", "pr", "view", "1", repo=None)


def test_a_body_that_mentions_a_protected_label_is_fine():
    assert plan("malek", "pr", "comment", "3", "--body", "waiting for reviewed-by-ahmed").permissions == PR_WRITE


# --- policy: git push ---------------------------------------------------------------------------

URL = "https://github.com/basitasolutions/basita-ops.git"


@pytest.mark.parametrize(
    "args, branch, planned",
    [
        ([], "DFP-1-x", ["origin", "HEAD:refs/heads/DFP-1-x"]),
        (["-u", "origin", "DFP-1-x"], "main", ["-u", "origin", "DFP-1-x:refs/heads/DFP-1-x"]),
        (["origin", "HEAD"], "DFP-1-x", ["origin", "HEAD:refs/heads/DFP-1-x"]),
        (["origin", "HEAD:refs/heads/DFP-2"], None, ["origin", "HEAD:refs/heads/DFP-2"]),
    ],
)
def test_push_plans_make_the_destination_explicit(args, branch, planned):
    result = plan_push(args, branch, URL, CONFIG)
    assert result.args == planned and result.permissions == PUSH and result.repo == "basitasolutions/basita-ops"


@pytest.mark.parametrize(
    "args, branch, why",
    [
        ([], "main", "pushing to main"),
        (["origin", "HEAD:main"], "x", "pushing to main"),
        (["origin", "x:refs/heads/Master"], "x", "pushing to Master"),
        (["origin", "main"], "x", "pushing to main"),
        (["--force", "origin", "x"], "x", "forced"),
        (["--force-with-lease=x", "origin", "x"], "x", "forced"),
        (["origin", "+x:x"], "x", "forced"),
        (["origin", ":x"], "x", "deleting"),
        (["--delete", "origin", "x"], "x", "deleting"),
        (["--mirror"], "x", "mirror"),
        (["--all"], "x", "bulk"),
        (["--tags"], "x", "tag"),
        (["origin", "refs/tags/v1"], "x", "branches only"),
        (["origin", "refs/*:refs/*"], "x", "wildcard"),
        # basita-ops sync.sh merges claude/* into main by itself, so an App never pushes one.
        ([], "claude/basita-149-x", "sync.sh merges claude"),
        (["origin", "HEAD:claude/x"], "x", "sync.sh merges claude"),
        (["origin", "x:refs/heads/Claude/x"], "x", "sync.sh merges Claude"),
        (["--receive-pack=x"], "x", "not on agent-git's list"),
        ([], None, "detached"),
    ],
)
def test_push_refusals(args, branch, why):
    with pytest.raises(IdentityError, match=why):
        plan_push(args, branch, URL, CONFIG)


def test_remotes():
    assert repo_from_remote("git@github.com:basitasolutions/basita-ops.git") == "basitasolutions/basita-ops"
    assert repo_from_remote("ssh://git@github.com/a/b") == "a/b"
    assert repo_from_remote("https://x@github.com/a/b.git") == "a/b"
    for bad in ("https://gitlab.com/a/b.git", "https://user:secret@github.com/a/b.git"):
        with pytest.raises(IdentityError):
            repo_from_remote(bad)


# --- identity: keys, JWT, tokens -----------------------------------------------------------------


def test_bot_email_and_unknown_agents():
    assert bot_email(agent_entry(CONFIG, "malek")) == "351705446+basita-malek[bot]@users.noreply.github.com"
    with pytest.raises(IdentityError, match="no GitHub identity"):
        agent_entry(CONFIG, "maya")


def test_private_key_must_be_base64_of_a_pem(key):
    pem, _ = key
    service = "basita-gh-malek-app"
    assert private_key(service, keychain({service: base64.b64encode(pem).decode()})) == pem
    for bad in ("not base64!", base64.b64encode(b"hello").decode()):
        with pytest.raises(IdentityError, match="does not hold"):
            private_key(service, keychain({service: bad}))
    with pytest.raises(IdentityError, match="no Keychain item"):
        private_key(service, keychain({}))


def test_jwt_is_signed_rs256_by_the_key(key, tmp_path):
    pem, public = key
    jwt = app_jwt(agent_entry(CONFIG, "malek"), pem, now=1_000_000)
    header, payload, signature = jwt.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "Iv23lixrAyMveN8xvGIK"}
    assert json.loads(base64.urlsafe_b64decode(header + "==")) == {"alg": "RS256", "typ": "JWT"}
    (tmp_path / "data").write_bytes(f"{header}.{payload}".encode())
    (tmp_path / "sig").write_bytes(base64.urlsafe_b64decode(signature + "=="))
    verify = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public),
            "-signature",
            str(tmp_path / "sig"),
            str(tmp_path / "data"),
        ],
        capture_output=True,
        text=True,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_app_token_is_for_the_listed_installation_and_one_repo(key):
    pem, _ = key
    github = FakeGitHub(
        {("POST", "/app/installations/177982765/access_tokens"): (201, {}, {"token": MINTED, "expires_at": "x"})}
    )
    run = keychain({"basita-gh-malek-app": base64.b64encode(pem).decode()})
    token = token_for("malek", "ahmedoraby-eng/engineering-playbook", PUSH, CONFIG, github, run)
    assert token == (MINTED, True)
    method, path, auth, body = github.calls[0]
    assert auth.startswith("Bearer ") and body == {"permissions": PUSH, "repositories": ["engineering-playbook"]}
    with pytest.raises(IdentityError, match="no listed installation for someone"):
        token_for("malek", "someone/repo", PUSH, CONFIG, github, run)
    with pytest.raises(IdentityError, match="no listed installation for ahmedoraby-eng"):
        token_for("haya", "ahmedoraby-eng/engineering-playbook", READ, CONFIG, github, run)


def test_github_errors_never_carry_the_token(key):
    pem, _ = key
    github = FakeGitHub(
        {("POST", "/app/installations/177981619/access_tokens"): (422, {}, {"message": "repository not installed"})}
    )
    run = keychain({"basita-gh-malek-app": base64.b64encode(pem).decode()})
    with pytest.raises(IdentityError, match="422 to POST /app/installations/177981619/access_tokens") as error:
        token_for("malek", "basitasolutions/other", READ, CONFIG, github, run)
    assert "Bearer" not in str(error.value)


def test_yousef_token_is_chosen_by_repo_owner():
    run = keychain({"basita-gh-yousef": "github_pat_ORG", "basita-gh-yousef-public": "ghp_PUBLIC"})
    # His personal tokens are never revoked after a command.
    assert token_for("yousef", "basitasolutions/basita-ops", READ, CONFIG, run=run) == ("github_pat_ORG", False)
    assert token_for("yousef", "ahmedoraby-eng/engineering-playbook", READ, CONFIG, run=run) == ("ghp_PUBLIC", False)
    with pytest.raises(IdentityError, match="no token for someone"):
        token_for("yousef", "someone/x", READ, CONFIG, run=run)


# --- gh-check -------------------------------------------------------------------------------------


def _app_answers(installations, repos):
    answers = {
        ("GET", "/app"): (
            200,
            {},
            {"slug": "basita-malek", "id": 5586450, "permissions": CONFIG["app_permissions"], "events": []},
        ),
        ("GET", "/users/basita-malek%5Bbot%5D"): (200, {}, {"id": 351705446}),
        ("GET", "/app/installations?per_page=100&page=1"): (200, {}, installations),
        ("GET", "/installation/repositories?per_page=100"): (200, {}, {"repositories": [{"name": r} for r in repos]}),
    }
    answers[("DELETE", "/installation/token")] = (204, {}, {})
    for i in installations:
        answers[("POST", f"/app/installations/{i['id']}/access_tokens")] = (
            201,
            {},
            {"token": MINTED, "expires_at": "2026-11-03T21:00:00Z"},
        )
    return answers


def test_gh_check_app_ok_and_flags_an_unknown_installation(key):
    pem, _ = key
    installations = [
        {
            "id": 177981619,
            "account": {"login": "basitasolutions"},
            "repository_selection": "selected",
            "permissions": {},
        },
        {"id": 999, "account": {"login": "stranger"}, "repository_selection": "all"},
    ]
    config = {**CONFIG, "agents": {"malek": {**CONFIG["agents"]["malek"]}}}
    config["agents"]["malek"]["installations"] = {
        "basitasolutions": CONFIG["agents"]["malek"]["installations"]["basitasolutions"]
    }
    github = FakeGitHub(_app_answers(installations, ["basita-ops", "elbayoumy-farm-nutrition-platform"]))
    run = keychain({"basita-gh-malek-app": base64.b64encode(pem).decode()})
    lines, code = run_check(config, ["malek"], github, run)
    text = "\n".join(lines)
    assert code == 0, text
    assert "ok key" in text and "ok App permissions" in text and "ok bot" in text
    assert "ok installation 177981619 on basitasolutions (selected): repos basita-ops, elbayoumy" in text
    assert "attention installation 999 on stranger is not in the config" in text
    bot_call = next(c for c in github.calls if c[1].startswith("/users/"))
    assert bot_call[2] == f"token {MINTED}"  # GitHub refuses a JWT on /users
    assert lines[3] == "  ok bot: basita-malek[bot] id 351705446"
    assert MINTED not in text
    token_requests = [c for c in github.calls if c[0] == "POST"]
    assert token_requests == [token_requests[0]] and token_requests[0][3] == {"permissions": {"metadata": "read"}}
    assert "ok webhook: none" in text  # GitHub's 404 means no webhook
    revokes = [c for c in github.calls if c[0] == "DELETE"]
    assert revokes == [("DELETE", "/installation/token", f"token {MINTED}", None)]  # the check token is revoked


def test_gh_check_app_fails_on_extra_permissions_missing_installation_and_other_repos(key):
    pem, _ = key
    answers = _app_answers([], [])
    answers[("GET", "/app")] = (
        200,
        {},
        {"slug": "basita-malek", "id": 5586450, "permissions": {**CONFIG["app_permissions"], "workflows": "write"}},
    )
    run = keychain({"basita-gh-malek-app": base64.b64encode(pem).decode()})
    lines, code = run_check(CONFIG, ["malek"], FakeGitHub(answers), run)
    text = "\n".join(lines)
    assert code == 1
    assert "FAIL App permissions are contents=write metadata=read pull_requests=write workflows=write" in text
    assert "FAIL installation 177981619 (basitasolutions) from the config is not installed" in text
    lines, code = run_check(CONFIG, ["haya"], FakeGitHub({}), keychain({}))
    assert code == 1 and "FAIL no Keychain item basita-gh-haya-app" in "\n".join(lines)


def test_gh_check_yousef_expiry_scopes_and_push():
    run = keychain({"basita-gh-yousef": "github_pat_ORG", "basita-gh-yousef-public": "ghp_PUBLIC"})
    org = {"github-authentication-token-expiration": "2026-11-10 00:00:00 UTC"}
    public = {"github-authentication-token-expiration": "2027-10-31 00:00:00 UTC", "x-oauth-scopes": "public_repo"}

    class ByToken(FakeGitHub):
        def __call__(self, method, url, headers, body):
            classic = headers["Authorization"].endswith("ghp_PUBLIC")
            self.answers[("GET", "/user")] = (200, public if classic else org, {"login": "basita-yousef"})
            return super().__call__(method, url, headers, body)

    repos = [{"full_name": "basitasolutions/basita-ops", "owner": {"login": "basitasolutions"}, "permissions": {}}]
    github = ByToken({("GET", "/user/repos?per_page=100"): (200, {}, repos)})
    lines, code = run_check(CONFIG, ["yousef"], github, run, today=dt.date(2026, 11, 3))
    text = "\n".join(lines)
    assert code == 0, text
    assert (
        "ok basita-gh-yousef (basitasolutions, fine-grained): login basita-yousef; expires 2026-11-10 (7 days)" in text
    )
    assert "repos basitasolutions/basita-ops (read)" in text
    assert "attention basita-gh-yousef expires in 7 days" in text
    assert "ok basita-gh-yousef-public (ahmedoraby-eng, classic)" in text and "scopes public_repo" in text
    assert "github_pat_ORG" not in text and "ghp_PUBLIC" not in text

    public["x-oauth-scopes"] = "public_repo, repo"
    repos[0]["permissions"] = {"push": True}
    lines, code = run_check(CONFIG, ["yousef"], github, run, today=dt.date(2026, 11, 3))
    text = "\n".join(lines)
    assert code == 1 and "Yousef must not push" in text and "(the config says public_repo)" in text


# --- the CLI and the git wrapper --------------------------------------------------------------------


def _env(tmp_path, **extra):
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        **extra,
    }


def _repo(tmp_path, branch="DFP-1-x"):
    repo = tmp_path / "repo"
    env = _env(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", URL], check=True, env=env)
    return repo


def test_agent_gh_dry_run_and_refusals_through_the_script(tmp_path):
    repo = _repo(tmp_path)
    env = _env(tmp_path, AGENT_GH_DRY_RUN="1")
    ok = subprocess.run(
        [str(ROOT / "scripts" / "agent-gh"), "malek", "pr", "view", "3"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0 and "on basitasolutions/basita-ops with contents=read" in ok.stdout
    bad = subprocess.run(
        [str(ROOT / "scripts" / "agent-gh"), "malek", "pr", "merge", "3"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1 and "refused: merging is Ahmed's" in bad.stderr


def test_agent_git_push_goes_through_agent_gh(tmp_path):
    repo = _repo(tmp_path, branch="malek/DFP-1-x")
    script = str(ROOT / "scripts" / "agent-git")
    env = _env(tmp_path, AGENT_GH_DRY_RUN="1")
    ok = subprocess.run([script, "malek", "push"], cwd=repo, env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and "origin HEAD:refs/heads/malek/DFP-1-x" in ok.stdout
    for args, why in (
        (["push", "origin", "main"], "pushing to main"),
        (["push", "origin", "HEAD:DFP-1-x"], "pushes only to malek/* branches"),
        (["-C", ".", "push"], "put push first"),
    ):
        bad = subprocess.run([script, "malek", *args], cwd=repo, env=env, capture_output=True, text=True)
        assert bad.returncode != 0 and why in bad.stderr
    # Yousef pushes through his documents App (BASITA-157), which keeps to its own prefix.
    bad = subprocess.run([script, "yousef", "push"], cwd=repo, env=env, capture_output=True, text=True)
    assert bad.returncode == 1 and "basita-yousef-docs pushes only to yousef/* branches" in bad.stderr


def test_agent_git_commits_with_the_bot_address(tmp_path):
    repo = _repo(tmp_path)
    script = str(ROOT / "scripts" / "agent-git")
    env = _env(tmp_path)
    subprocess.run([script, "malek", "commit", "-q", "--allow-empty", "-m", "DFP-1: x"], cwd=repo, env=env, check=True)
    subprocess.run(
        [script, "talia", "commit", "-q", "--allow-empty", "-m", "BASITA-1: y"], cwd=repo, env=env, check=True
    )
    subprocess.run(
        [script, "salma", "commit", "-q", "--allow-empty", "-m", "DFP-2: z"],
        cwd=repo,
        env={**env, "AGENT_GIT_LEGACY_EMAIL": "1"},
        check=True,
    )
    log = subprocess.run(
        ["git", "log", "--format=%an <%ae>|%ce|%(trailers:key=Agent,valueonly)"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    salma, talia, malek = [line for line in log.splitlines() if line]
    assert malek.startswith("Malek (Basita AI) <351705446+basita-malek[bot]@users.noreply.github.com>|351705446+")
    assert malek.endswith("|Malek")
    assert talia.startswith("Talia (Basita AI) <351996843+basita-talia[bot]@users.noreply.github.com>|351996843+")
    assert salma.startswith("Salma (Basita AI) <salma@basitasolutions.com>")


def test_the_credential_helper_answers_with_the_agent_token_only(tmp_path):
    """The one-command helper replaces every helper in the user's git config (gh's, the Keychain's)."""
    (tmp_path / ".gitconfig").write_text(
        '[credential "https://github.com"]\n\thelper = "!f() { echo password=AHMEDS; }; f"\n'
        '[credential]\n\thelper = "!f() { echo password=KEYCHAIN; }; f"\n'
    )
    env = _env(tmp_path, AGENT_GH_TOKEN="agent-token", GIT_TERMINAL_PROMPT="0")
    fill = subprocess.run(
        ["git", "-c", "credential.helper=", "-c", f"credential.helper={cli.HELPER}", "credential", "fill"],
        input="protocol=https\nhost=github.com\npath=a/b.git\n\n",
        env=env,
        capture_output=True,
        text=True,
    )
    assert fill.returncode == 0
    assert [line for line in fill.stdout.splitlines() if line.startswith("password=")] == ["password=agent-token"]


def test_cli_usage_and_bot_email(capsys):
    assert cli.main([]) == 2
    assert cli.main(["bot-email", "haya"]) == 0
    assert capsys.readouterr().out.strip() == "351706579+basita-haya[bot]@users.noreply.github.com"
    assert cli.main(["bot-email", "talia"]) == 0  # created at S2 (BASITA-155)
    assert capsys.readouterr().out.strip() == "351996843+basita-talia[bot]@users.noreply.github.com"
    assert cli.main(["gh", "maya", "pr", "view"]) == 1
    assert "maya has no GitHub identity" in capsys.readouterr().err


def test_store_keeps_only_a_key_and_never_prints_it(key, tmp_path, monkeypatch, capsys):
    pem, _ = key
    (tmp_path / "k.pem").write_bytes(pem)
    (tmp_path / "bad.pem").write_text("hello")
    stored = []

    def fake_run(cmd, **_):
        if cmd[0] == "security":
            assert cmd == ["security", "-i"]  # the value goes on stdin, never in the process list
            stored.append(_["input"].split())
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "pbpaste":
            return subprocess.CompletedProcess(cmd, 0, "ghp_abc" if _.get("text") else b"", "")
        raise AssertionError(cmd)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["store", "malek", "--file", str(tmp_path / "k.pem")]) == 0
    assert stored[-1][:5] == ["add-generic-password", "-U", "-s", "basita-gh-malek-app", "-a"]
    assert base64.b64decode(stored[-1][-1].strip('"')) == pem
    assert cli.main(["store", "malek", "--file", str(tmp_path / "bad.pem")]) == 1
    assert cli.main(["store", "yousef", "ahmedoraby-eng"]) == 0
    assert stored[-1][3] == "basita-gh-yousef-public" and stored[-1][-1] == '"ghp_abc"'
    assert cli.main(["store", "yousef", "basitasolutions"]) == 1  # a classic token is not fine-grained
    out = capsys.readouterr().out
    assert "ghp_abc" not in out and "PRIVATE KEY" not in out and len(stored) == 2


# --- Yousef's review of #26 ---------------------------------------------------------------------------


def _fill(env, host, protocol="https"):
    return subprocess.run(
        ["git", "-c", "credential.helper=", "-c", f"credential.helper={cli.HELPER}", "credential", "fill"],
        input=f"protocol={protocol}\nhost={host}\npath=a/b.git\n\n",
        env=env,
        capture_output=True,
        text=True,
    )


def test_the_credential_helper_answers_only_https_github_com(tmp_path):
    env = _env(tmp_path, AGENT_GH_TOKEN="agent-token", GIT_TERMINAL_PROMPT="0")
    assert "password=agent-token" in _fill(env, "github.com").stdout
    for host, protocol in (("evil.example", "https"), ("github.com.evil.example", "https"), ("github.com", "http")):
        result = _fill(env, host, protocol)
        assert "agent-token" not in result.stdout and result.returncode != 0


def test_a_remote_with_two_push_urls_is_refused(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    env = _env(tmp_path, AGENT_GH_DRY_RUN="1")
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "--add", "--push", "origin", URL], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "set-url", "--add", "--push", "origin", "https://evil.example/x.git"],
        check=True,
        env=env,
    )
    bad = subprocess.run(
        [str(ROOT / "scripts" / "agent-git"), "malek", "push"], cwd=repo, env=env, capture_output=True, text=True
    )
    assert bad.returncode == 1 and "has 2 push URLs" in bad.stderr


def test_trace_and_config_variables_are_cleared(monkeypatch):
    for name in (
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE_PACKET",
        "GIT_TRACE_REDACT",
        "GIT_CURL_VERBOSE",
        "GH_DEBUG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GITHUB_TOKEN",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("KEEP_ME", "1")
    env = cli._env()
    assert not [k for k in env if k.startswith(("GIT_TRACE", "GIT_CURL", "GH_DEBUG", "GIT_CONFIG", "GITHUB_TOKEN"))]
    assert env["KEEP_ME"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"


class Revocations:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.replace("https://api.github.com", ""), headers["Authorization"]))
        return 204, {}, ""


@pytest.mark.parametrize("command, code", [(["true"], 0), (["false"], 1), (["sh", "-c", "exit 7"], 7)])
def test_an_app_token_is_revoked_after_the_command_even_when_it_fails(command, code):
    github = Revocations()
    assert cli.run_with_token(command, {"PATH": os.environ["PATH"]}, MINTED, True, github) == code
    assert github.calls == [("DELETE", "/installation/token", f"token {MINTED}")]


def test_yousef_token_is_not_revoked_and_a_failed_revoke_is_said(capsys):
    github = Revocations()
    assert cli.run_with_token(["true"], {"PATH": os.environ["PATH"]}, "ghp_PUBLIC", False, github) == 0
    assert github.calls == []

    def refuses(method, url, headers, body):
        return 401, {}, '{"message": "Bad credentials"}'

    assert cli.run_with_token(["true"], {"PATH": os.environ["PATH"]}, MINTED, True, refuses) == 0
    err = capsys.readouterr().err
    assert "could not revoke" in err and MINTED not in err


def test_cmd_gh_runs_gh_and_revokes(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: (MINTED, True))
    monkeypatch.setattr(cli, "_default_repo", lambda: "basitasolutions/basita-ops")
    monkeypatch.setattr(
        cli, "run_with_token", lambda cmd, env, token, revocable: calls.append((cmd, env, revocable)) or 3
    )
    monkeypatch.delenv("AGENT_GH_DRY_RUN", raising=False)
    assert cli.cmd_gh("malek", ["pr", "view", "3"]) == 3
    cmd, env, revocable = calls[0]
    assert cmd == ["gh", "pr", "view", "3"] and revocable
    assert env["GH_TOKEN"] == MINTED and env["GH_REPO"] == "basitasolutions/basita-ops"


def test_cmd_push_never_pushes_submodules(monkeypatch, tmp_path):
    calls = []
    monkeypatch.chdir(_repo(tmp_path, branch="malek/DFP-1-x"))
    monkeypatch.setattr(cli, "token_for", lambda *a, **k: (MINTED, True))
    monkeypatch.setattr(cli, "run_with_token", lambda cmd, env, token, revocable: calls.append((cmd, env)) or 0)
    monkeypatch.delenv("AGENT_GH_DRY_RUN", raising=False)
    assert cli.cmd_push("malek", []) == 0
    cmd, env = calls[0]
    assert "push.recurseSubmodules=no" in cmd and cmd[-3:] == ["push", "origin", "HEAD:refs/heads/malek/DFP-1-x"]
    assert env["AGENT_GH_TOKEN"] == MINTED and MINTED not in " ".join(cmd)


@pytest.mark.parametrize(
    "args, why",
    [
        (["pr", "review", "3", "--approve=true"], "Yousef reviews"),
        (["pr", "review", "3", "-ab", "ok"], "Yousef reviews"),
        (["pr", "edit", "3", "--base", "dev"], "retargeting"),
        (["pr", "edit", "3", "-B", "dev"], "retargeting"),
        (["api", "repos/a/b/issues/3/labels", "-F", "labels[]=@labels.txt"], "from a file"),
        (["api", "repos/a/b/pulls/3/reviews", "-F", "event=@event.txt"], "from a file"),
    ],
)
def test_review_hardening_refusals(args, why):
    with pytest.raises(IdentityError, match=why):
        plan("malek", *args)


def test_yousef_only_reads_reviews_and_comments():
    for args in (
        ["pr", "review", "3", "--request-changes", "-b", "x"],
        ["pr", "comment", "3", "-b", "x"],
        ["issue", "comment", "3", "-b", "x"],
        ["api", "repos/a/b/pulls/3/reviews", "-f", "event=APPROVE"],
    ):
        assert plan("yousef", *args).permissions == PR_WRITE
    for args in (
        ["pr", "create", "-t", "x"],
        ["pr", "edit", "3", "-t", "x"],
        ["pr", "ready", "3"],
        ["api", "repos/a/b/pulls", "-f", "title=x"],
        ["api", "repos/a/b/issues/3/labels", "-f", "labels[]=x"],
    ):
        with pytest.raises(IdentityError, match="not on agent-gh's list"):
            plan("yousef", *args)


def test_gh_check_flags_a_webhook_and_pages_installations(key):
    pem, _ = key
    first = [{"id": 1000 + i, "account": {"login": f"stranger{i}"}} for i in range(100)]
    answers = _app_answers([], [])
    answers[("GET", "/app/installations?per_page=100&page=1")] = (200, {}, first)
    answers[("GET", "/app/installations?per_page=100&page=2")] = (200, {}, [{"id": 7, "account": {"login": "last"}}])
    answers[("GET", "/app/hook/config")] = (200, {}, {"url": "https://example.com/hook", "content_type": "json"})
    run = keychain({"basita-gh-malek-app": base64.b64encode(pem).decode()})
    lines, code = run_check(CONFIG, ["malek"], FakeGitHub(answers), run)
    text = "\n".join(lines)
    assert code == 1 and "FAIL a webhook is configured" in text and "example.com" not in text
    assert "attention installation 7 on last" in text and text.count("attention installation") == 101


@pytest.mark.parametrize(
    ("deliveries", "expected"),
    [
        ([{"event": "ping"}], "ok webhook: inactive"),
        ([], "ok webhook: inactive"),
        ([{"event": "ping"}, {"event": "installation"}], "FAIL the webhook is active: GitHub delivered installation"),
    ],
)
def test_gh_check_reads_gh_app_new_placeholder_hook_by_its_deliveries(key, deliveries, expected):
    pem, _ = key
    answers = _app_answers([], [])
    answers[("GET", "/app/hook/config")] = (
        200,
        {},
        {"url": "https://github.com/basitasolutions", "content_type": "json"},
    )
    answers[("GET", "/app/hook/deliveries?per_page=100")] = (200, {}, deliveries)
    run = keychain({"basita-gh-malek-app": base64.b64encode(pem).decode()})
    lines, _ = run_check(CONFIG, ["malek"], FakeGitHub(answers), run)
    assert expected in "\n".join(lines)


def test_gh_check_fails_when_yousef_is_a_collaborator_on_the_playbook():
    run = keychain({"basita-gh-yousef-public": "ghp_PUBLIC"})
    headers = {"github-authentication-token-expiration": "2027-10-31 00:00:00 UTC", "x-oauth-scopes": "public_repo"}
    repos = [
        {
            "full_name": "ahmedoraby-eng/engineering-playbook",
            "owner": {"login": "ahmedoraby-eng"},
            "permissions": {"push": True},
        }
    ]
    github = FakeGitHub(
        {
            ("GET", "/user"): (200, headers, {"login": "basita-yousef"}),
            ("GET", "/user/repos?per_page=100"): (200, {}, repos),
        }
    )
    config = {**CONFIG, "agents": {"yousef": {**CONFIG["agents"]["yousef"]}}}
    config["agents"]["yousef"]["tokens"] = {"ahmedoraby-eng": CONFIG["agents"]["yousef"]["tokens"]["ahmedoraby-eng"]}
    lines, code = run_check(config, ["yousef"], github, run, today=dt.date(2026, 11, 3))
    assert code == 1 and "ahmedoraby-eng/engineering-playbook (push)" in "\n".join(lines)
