"""BASITA-157: the seven new identities, their branch prefixes, sync, reader and Yousef's push App.

One test for every refusal. Ahmed created the seven Apps at S2 (BASITA-155), so the real config has their
ids. The tests that need an App set up use a copy of the config with made-up ids, and the tests of a
pending App use a copy with the ids taken out (no real key or token is used anywhere).
"""

import base64
import copy
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import agent_gh.identity as identity
from agent_attribution import Commit, PullRequest, check
from agent_attribution.__main__ import load_bots
from agent_attribution.check import Config
from agent_gh import __main__ as cli
from agent_gh.check import run_check
from agent_gh.identity import (
    PR_WRITE,
    PUSH,
    READ,
    IdentityError,
    acting_identity,
    agent_entry,
    is_pending,
    load_config,
    read_permissions,
    token_for,
)
from agent_gh.policy import plan_gh, plan_push, uses_push_app

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "github-identities.json"
CONFIG = load_config(CONFIG_FILE)
MINTED = "ghs_fake-installation-value"  # a stand-in, not a real token
OPS = "https://github.com/basitasolutions/basita-ops.git"
FARM = "https://github.com/basitasolutions/elbayoumy-farm-nutrition-platform.git"
PLAYBOOK = "https://github.com/ahmedoraby-eng/engineering-playbook.git"
NEW = ("talia", "sherif", "adam", "nora", "yousef-docs", "sync", "reader")
PREFIXED = {
    "malek": "malek/",
    "haya": "haya/",
    "salma": "salma/",
    "talia": "talia/",
    "sherif": "sherif/",
    "adam": "adam/",
    "nora": "nora/",
    "yousef-docs": "yousef/",
}
READER_PERMISSIONS = {
    "actions": "read",
    "checks": "read",
    "contents": "read",
    "issues": "read",
    "metadata": "read",
    "pull_requests": "read",
    "statuses": "read",
}


def ready(config=CONFIG, *names):
    """A copy of the config in which the named Apps are set up, with made-up ids and installations."""
    config = copy.deepcopy(config)
    for number, name in enumerate(names or NEW, start=1):
        entry = config["agents"][name]
        entry.update({"app_id": 9000 + number, "client_id": f"Iv-test-{number}", "bot_id": 8000 + number})
        entry["installations"] = {
            "basitasolutions": {"id": 7000 + number, "repos": ["basita-ops"]},
            "ahmedoraby-eng": {"id": 6000 + number, "repos": ["engineering-playbook"]},
        }
    return config


READY = ready()


def pending(config=CONFIG, *names):
    """A copy of the config in which the named Apps are not created yet, as before S2."""
    config = copy.deepcopy(config)
    for name in names or NEW:
        config["agents"][name].update({"app_id": None, "client_id": None, "bot_id": None, "installations": {}})
    return config


PENDING = pending()

# S2 (BASITA-155): app_id, client_id, bot_id and installations, from basita-ops
# work/talia/github-apps-created-2026-11-03.md.
CREATED = {
    "talia": (5596031, "Iv23li8erVR1kSm1QyTh", 351996843, {"basitasolutions": 178208723, "ahmedoraby-eng": 179709236}),
    "sherif": (5596085, "Iv23lirm0mILYg7g4HeY", 351998495, {"basitasolutions": 178209669, "ahmedoraby-eng": 179708963}),
    "adam": (5596141, "Iv23libZR9pm7kX377jm", 352000396, {"basitasolutions": 178210706, "ahmedoraby-eng": 179708242}),
    "nora": (5596187, "Iv23lixUCYzrGsGWZhIE", 352001471, {"basitasolutions": 178211748, "ahmedoraby-eng": 179709305}),
    "yousef-docs": (5596240, "Iv23liePPnWTo9uCmrRH", 352002984, {"basitasolutions": 178212701, "ahmedoraby-eng": 179709388}),
    "sync": (5596339, "Iv23liWjBt4dzcm7Ufye", 352005823, {"basitasolutions": 178214976}),
    "reader": (
        5596397,
        "Iv23lig3YQPy8TNoAlGM",
        352007377,
        {"basitasolutions": 178216385, "ahmedoraby-eng": 178216933},
    ),
}


def entry(name, config=READY):
    return agent_entry(config, name)


def push(name, args, branch="x", url=OPS, config=READY):
    return plan_push(args, branch, url, config, entry(name, config))


def keychain(values):
    def run(cmd, **_):
        service = cmd[cmd.index("-s") + 1]
        found = values.get(service)
        return subprocess.CompletedProcess(cmd, 0 if found else 44, stdout=found or "", stderr="")

    return run


def no_keychain(cmd, **_):
    raise AssertionError(f"the Keychain was read: {cmd}")


class FakeGitHub:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.replace("https://api.github.com", "")
        self.calls.append((method, path, headers.get("Authorization", ""), json.loads(body) if body else None))
        status, response_headers, payload = self.answers.get((method, path), (404, {}, {"message": "Not Found"}))
        return status, response_headers, json.dumps(payload)


@pytest.fixture(scope="module")
def pem(tmp_path_factory):
    folder = tmp_path_factory.mktemp("key")
    subprocess.run(["openssl", "genrsa", "-out", str(folder / "k.pem"), "2048"], check=True, capture_output=True)
    return (folder / "k.pem").read_bytes()


def _playbook_copy(tmp_path, config, name="playbook"):
    """The playbook's scripts and code with the given config, so the scripts run against it."""
    copy_root = tmp_path / name
    for folder in ("scripts", "src"):
        shutil.copytree(ROOT / folder, copy_root / folder, ignore=shutil.ignore_patterns("__pycache__"))
    (copy_root / "config").mkdir()
    (copy_root / "config" / "github-identities.json").write_text(json.dumps(config))
    return copy_root


# --- the config -------------------------------------------------------------------------------------


def test_the_seven_apps_are_listed_with_the_ids_of_s2():
    agents = CONFIG["agents"]
    for name in NEW:
        app = agents[name]
        assert app["kind"] == "app" and app["slug"] == f"basita-{name}"
        app_id, client_id, bot_id, installations = CREATED[name]
        assert (app["app_id"], app["client_id"], app["bot_id"]) == (app_id, client_id, bot_id)
        assert {owner: i["id"] for owner, i in app["installations"].items()} == installations
        assert app["keychain"] == f"basita-gh-{name}-app"
        assert app["manifest"]["name"] == app["slug"] and app["manifest"]["description"]
        assert not is_pending(app) and is_pending(PENDING["agents"][name])
    assert agents["talia"]["installations"]["basitasolutions"]["repos"] == [
        "basita-ops",
        "elbayoumy-farm-nutrition-platform",
    ]
    assert agents["reader"]["installations"]["ahmedoraby-eng"]["repos"] == ["engineering-playbook"]
    for name in ("sherif", "adam", "nora", "yousef-docs", "sync"):
        assert agents[name]["installations"]["basitasolutions"]["repos"] == ["basita-ops"], name
    for name, prefix in PREFIXED.items():
        assert agents[name]["branch_prefix"] == prefix
    assert "branch_prefix" not in agents["sync"] and "branch_prefix" not in agents["reader"]
    assert agents["sync"]["updates_main"] == "basitasolutions/basita-ops"
    assert agents["reader"]["read_only"] is True and agents["reader"]["permissions"] == READER_PERMISSIONS
    assert {n for n, a in agents.items() if a.get("kind") == "app" and a["manifest"]["public"]} == {
        "malek", "reader", "haya", "salma", "talia", "sherif", "adam", "nora", "yousef-docs"
    }  # Ahmed 08/11: public so they install on the playbook


def test_the_four_live_identities_keep_every_field_they_had():
    live = {
        "malek": (
            5586450,
            "Iv23lixrAyMveN8xvGIK",
            351705446,
            {"basitasolutions": 177981619, "ahmedoraby-eng": 177982765},
        ),
        "haya": (5586509, "Iv23lilUPyDFpb9xrvRG", 351706579, {"basitasolutions": 177982074, "ahmedoraby-eng": 179709069}),  # + basita-ops, S2
        "salma": (5586535, "Iv23liIFJsD9Ub2uKUkh", 351707078, {"basitasolutions": 177982412, "ahmedoraby-eng": 179709156}),
    }
    for name, (app_id, client_id, bot_id, installations) in live.items():
        app = CONFIG["agents"][name]
        assert (app["app_id"], app["client_id"], app["bot_id"]) == (app_id, client_id, bot_id)
        assert {owner: i["id"] for owner, i in app["installations"].items()} == installations
        assert "permissions" not in app  # the top-level app_permissions still applies, as before
    yousef = CONFIG["agents"]["yousef"]
    assert yousef["kind"] == "user" and yousef["login"] == "basita-yousef" and yousef["push_app"] == "yousef-docs"
    assert set(yousef["tokens"]) == {"basitasolutions", "ahmedoraby-eng"}
    assert CONFIG["protected_prefixes"] == ["claude/"] and CONFIG["protected_branches"] == ["main", "master"]


def test_no_app_may_hold_workflows_administration_secrets_or_write_actions():
    """D4 and plan 10.2. sync has no issues permission until Ahmed grants it on GitHub (D16 with D22)."""
    for name, app in CONFIG["agents"].items():
        if app.get("kind") != "app":
            continue
        permissions = app.get("permissions") or CONFIG["app_permissions"]
        assert not {"workflows", "administration", "secrets", "variables"} & set(permissions), name
        assert permissions.get("actions", "read") == "read" and permissions["metadata"] == "read", name
    assert "issues" not in CONFIG["agents"]["sync"]["permissions"]
    assert CONFIG["agents"]["talia"]["permissions"]["issues"] == "read"


# --- pending ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEW)
def test_a_pending_app_is_refused_before_anything_is_planned_or_minted(name):
    with pytest.raises(IdentityError, match=r"refused: .* is not set up yet \(Ahmed's setup S2\)"):
        acting_identity(PENDING, "yousef" if name == "yousef-docs" else name, True)
    if name != "yousef-docs":
        with pytest.raises(IdentityError, match="not set up yet"):
            token_for(name, "basitasolutions/basita-ops", READ, PENDING, FakeGitHub({}), no_keychain)


def test_pending_through_the_scripts(tmp_path):
    copy_root = _playbook_copy(tmp_path, PENDING)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "talia/BASITA-1-x", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", OPS], check=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "AGENT_GH_DRY_RUN": "1"}
    for command in (
        ["agent-gh", "talia", "pr", "view", "3"],
        ["agent-gh", "reader", "pr", "view", "3"],
        ["agent-gh", "yousef", "pr", "create", "-t", "x"],
        ["agent-git", "talia", "push"],
        ["agent-git", "sync", "push", "origin", "main"],
        ["agent-git", "yousef", "push"],
    ):
        result = subprocess.run(
            [str(copy_root / "scripts" / command[0]), *command[1:]], cwd=repo, env=env, capture_output=True, text=True
        )
        assert result.returncode == 1 and "not set up yet (Ahmed's setup S2)" in result.stderr, command


# --- branch prefixes ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name, prefix", sorted(PREFIXED.items()))
def test_an_app_pushes_only_to_its_own_prefix(name, prefix):
    assert push(name, [], branch=f"{prefix}BASITA-1-x").permissions == PUSH
    assert push(name, [], branch=f"{prefix}BASITA-1-x").args == ["origin", f"HEAD:refs/heads/{prefix}BASITA-1-x"]
    assert push(name, ["origin", f"x:refs/heads/{prefix}y"]).args == ["origin", f"x:refs/heads/{prefix}y"]
    other = "nora/" if prefix != "nora/" else "adam/"
    for args, branch in (
        ([], "BASITA-1-x"),  # no prefix: the old free-for-all is gone
        ([], f"{other}BASITA-1-x"),  # another agent's prefix
        (["origin", f"HEAD:{prefix}"], "x"),  # the bare prefix is not a branch of its own
        (["origin", f"HEAD:{prefix.capitalize()}x"], "x"),  # the prefix is matched exactly
        (["origin", f"HEAD:refs/heads/x{prefix}y"], "x"),
        (["origin", f"{prefix}a", "other"], f"{prefix}a"),  # every refspec is checked
    ):
        with pytest.raises(IdentityError, match=f"pushes only to {prefix}\\* branches"):
            push(name, args, branch=branch)


@pytest.mark.parametrize("name", sorted(PREFIXED))
def test_every_app_is_still_refused_main_master_claude_and_force(name):
    prefix = PREFIXED[name]
    for args, branch, why in (
        ([], "main", "pushing to main"),
        (["origin", "HEAD:master"], "x", "pushing to master"),
        (["origin", "HEAD:claude/x"], "x", "sync.sh merges claude"),
        ([], "claude/basita-157-x", "sync.sh merges claude"),
        (["--force", "origin", f"{prefix}x"], "x", "forced"),
        (["origin", f"+HEAD:{prefix}x"], "x", "forced"),
        (["origin", f":{prefix}x"], "x", "deleting"),
    ):
        with pytest.raises(IdentityError, match=why):
            push(name, args, branch=branch)


def test_without_an_identity_the_shared_rules_alone_apply():
    """plan_push without an entry is the BASITA-149 behaviour: any branch but main, master and claude/*."""
    assert plan_push([], "DFP-1-x", OPS, READY).args == ["origin", "HEAD:refs/heads/DFP-1-x"]


# --- sync ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, branch, planned",
    [
        ([], "main", ["origin", "HEAD:refs/heads/main"]),
        (["origin", "main"], "x", ["origin", "main:refs/heads/main"]),
        (["origin", "HEAD:refs/heads/main"], "x", ["origin", "HEAD:refs/heads/main"]),
        (["-u", "origin", "HEAD:main"], None, ["-u", "origin", "HEAD:refs/heads/main"]),
    ],
)
def test_sync_updates_main_of_basita_ops(args, branch, planned):
    result = push("sync", args, branch=branch)
    assert result.args == planned and result.repo == "basitasolutions/basita-ops" and result.permissions == PUSH


@pytest.mark.parametrize(
    "args, branch, url, why",
    [
        ([], "main", FARM, "pushes only to main of basitasolutions/basita-ops, not to basitasolutions/elbayoumy"),
        ([], "main", PLAYBOOK, "not to ahmedoraby-eng/engineering-playbook"),
        ([], "sync/x", OPS, "pushing to sync/x; basita-sync pushes only main"),
        (["origin", "HEAD:master"], "x", OPS, "pushing to master; basita-sync pushes only main"),
        (["origin", "HEAD:Main"], "x", OPS, "pushing to Main; basita-sync pushes only main"),
        (["origin", "HEAD:talia/x"], "x", OPS, "basita-sync pushes only main"),
        (["origin", "HEAD:claude/x"], "x", OPS, "basita-sync pushes only main"),
        (["origin", "main", "HEAD:other"], "x", OPS, "pushing to other"),
        (["origin", "+HEAD:main"], "x", OPS, "forced"),
        (["--force", "origin", "main"], "x", OPS, "forced"),
        (["--force-with-lease", "origin", "main"], "x", OPS, "forced"),
        (["--force-if-includes", "origin", "main"], "x", OPS, "forced"),
        (["origin", ":main"], "x", OPS, "deleting"),
        (["--delete", "origin", "main"], "x", OPS, "deleting"),
        (["origin", "refs/tags/v1"], "x", OPS, "branches only"),
        (["--tags"], "main", OPS, "tag"),
        (["--mirror"], "main", OPS, "mirror"),
    ],
)
def test_sync_refusals(args, branch, url, why):
    with pytest.raises(IdentityError, match=why):
        push("sync", args, branch=branch, url=url)


def test_only_sync_updates_main():
    for name in PREFIXED:
        with pytest.raises(IdentityError, match="pushing to main; only Ahmed"):
            push(name, ["origin", "HEAD:main"])


def test_sync_runs_no_gh_command():
    for args in (["pr", "view", "3"], ["pr", "create", "-t", "x"], ["api", "repos/{owner}/{repo}/pulls"]):
        with pytest.raises(IdentityError, match="basita-sync only pushes main of basitasolutions/basita-ops"):
            plan_gh("sync", entry("sync"), args, "basitasolutions/basita-ops", READY)


def test_an_identity_with_both_a_prefix_and_main_is_refused():
    config = copy.deepcopy(READY)
    config["agents"]["sync"]["branch_prefix"] = "sync/"
    with pytest.raises(IdentityError, match="both a branch_prefix and updates_main"):
        push("sync", [], branch="main", config=config)


# --- reader -------------------------------------------------------------------------------------------


def test_reader_reads_with_its_own_read_permissions():
    reader = entry("reader")
    for args in (
        ["pr", "view", "3"],
        ["pr", "checks", "3"],
        ["run", "list"],
        ["api", "repos/{owner}/{repo}/pulls/3"],
        ["api", "graphql", "-f", "query={ viewer { login } }"],
    ):
        assert plan_gh("reader", reader, args, "basitasolutions/basita-ops", READY).permissions == READER_PERMISSIONS
    assert plan_gh("malek", entry("malek"), ["pr", "view", "3"], "a/b", READY).permissions == READ  # unchanged


@pytest.mark.parametrize(
    "args, why",
    [
        (["pr", "create", "-t", "x"], "basita-reader reads only"),
        (["pr", "edit", "3", "-t", "x"], "basita-reader reads only"),
        (["pr", "comment", "3", "-b", "x"], "basita-reader reads only"),
        (["pr", "review", "3", "--comment", "-b", "x"], "basita-reader reads only"),
        (["pr", "ready", "3"], "basita-reader reads only"),
        (["issue", "comment", "3", "-b", "x"], "basita-reader reads only"),
        (["api", "repos/a/b/issues/3/comments", "-f", "body=x"], "basita-reader reads only"),
        (["api", "-X", "PATCH", "repos/a/b/pulls/3", "-f", "title=x"], "basita-reader reads only"),
        (["api", "-X", "DELETE", "repos/a/b/git/refs/heads/x"], "basita-reader reads only"),
        (["api", "graphql", "-f", "query=mutation { x }"], "mutations"),
        (["pr", "merge", "3"], "merging is Ahmed's"),
    ],
)
def test_reader_refusals(args, why):
    with pytest.raises(IdentityError, match=why):
        plan_gh("reader", entry("reader"), args, "basitasolutions/basita-ops", READY)


@pytest.mark.parametrize("args", [[], ["origin", "HEAD:reader/x"], ["origin", "main"]])
def test_reader_never_pushes(args):
    with pytest.raises(IdentityError, match="basita-reader reads only; it never pushes"):
        push("reader", args)


def test_a_reader_given_a_write_permission_in_the_config_still_gets_no_writing_token(pem):
    config = copy.deepcopy(READY)
    config["agents"]["reader"]["permissions"]["contents"] = "write"
    with pytest.raises(IdentityError, match="read-only, but its config permissions are not all read"):
        read_permissions(config["agents"]["reader"])
    github = FakeGitHub({})
    with pytest.raises(IdentityError, match="reads only; it gets no token that writes"):
        token_for("reader", "basitasolutions/basita-ops", PUSH, READY, github, no_keychain)
    assert github.calls == []


# --- Yousef's push App ------------------------------------------------------------------------------------


def test_yousef_pushes_and_opens_prs_as_his_docs_app_and_reviews_as_himself():
    assert uses_push_app(["pr", "create", "-t", "x"]) and uses_push_app(["pr", "edit", "3"])
    for args in (["pr", "review", "3", "--approve"], ["pr", "comment", "3"], ["issue", "comment", "3"], ["pr"]):
        assert not uses_push_app(args)
    name, docs = acting_identity(READY, "yousef", True)
    assert name == "yousef-docs" and docs["slug"] == "basita-yousef-docs"
    name, user = acting_identity(READY, "yousef", False)
    assert name == "yousef" and user["kind"] == "user"
    assert plan_gh("yousef-docs", docs, ["pr", "create", "-t", "x"], "a/b", READY).permissions == PR_WRITE
    assert push("yousef-docs", [], branch="yousef/BASITA-1-plan").args == [
        "origin",
        "HEAD:refs/heads/yousef/BASITA-1-plan",
    ]


@pytest.mark.parametrize(
    "config, agent, why",
    [
        (PENDING, "yousef", "basita-yousef-docs is not set up yet"),  # his push App before S2
        (READY, "yousef-docs", "basita-yousef-docs is yousef's; use agent-gh yousef or agent-git yousef"),
    ],
)
def test_yousef_push_app_refusals(config, agent, why):
    with pytest.raises(IdentityError, match=why):
        acting_identity(config, agent, True)


def test_a_user_without_a_push_app_still_does_not_push():
    config = copy.deepcopy(READY)
    del config["agents"]["yousef"]["push_app"]
    with pytest.raises(IdentityError, match="basita-yousef reviews and doesn't push"):
        acting_identity(config, "yousef", True)


def test_yousefs_docs_app_cannot_approve_and_keeps_the_app_rules():
    docs = entry("yousef-docs")
    for args, why in (
        (["pr", "review", "3", "--approve"], "an App does not approve"),
        (["pr", "edit", "3", "--add-label", "reviewed-by-ahmed"], "only by Ahmed"),
        (["pr", "edit", "3", "--base", "x"], "retargeting"),
    ):
        with pytest.raises(IdentityError, match=why):
            plan_gh("yousef-docs", docs, args, "a/b", READY)
    for branch in ("docs-x", "claude/x", "main"):
        with pytest.raises(IdentityError):
            push("yousef-docs", [], branch=branch)


# --- the CLI with Apps set up (a copy of the playbook with a ready config) -------------------------------------


@pytest.fixture
def playbook(tmp_path):
    """The playbook's scripts and code, with a config in which the new Apps are set up."""
    return _playbook_copy(tmp_path, READY)


def _clone(tmp_path, branch, url=OPS):
    repo = tmp_path / f"repo-{branch.replace('/', '-')}-{url.rsplit('/', 1)[1][:-4]}"
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", url], check=True)
    return repo


def _run(playbook, tmp_path, repo, *command, **extra):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "AGENT_GH_DRY_RUN": "1", **extra}
    return subprocess.run(
        [str(playbook / "scripts" / command[0]), *command[1:]], cwd=repo, env=env, capture_output=True, text=True
    )


def test_set_up_apps_through_the_scripts(playbook, tmp_path):
    talia = _clone(tmp_path, "talia/BASITA-1-notes")
    ok = _run(playbook, tmp_path, talia, "agent-git", "talia", "push")
    assert ok.returncode == 0 and "talia push on basitasolutions/basita-ops" in ok.stdout
    bad = _run(playbook, tmp_path, talia, "agent-git", "talia", "push", "origin", "HEAD:malek/x")
    assert bad.returncode == 1 and "basita-talia pushes only to talia/* branches" in bad.stderr

    docs = _clone(tmp_path, "yousef/BASITA-1-plan")
    ok = _run(playbook, tmp_path, docs, "agent-git", "yousef", "push")
    assert (
        ok.returncode == 0 and "yousef-docs push" in ok.stdout and "HEAD:refs/heads/yousef/BASITA-1-plan" in ok.stdout
    )
    ok = _run(playbook, tmp_path, docs, "agent-gh", "yousef", "pr", "create", "-t", "BASITA-1: plan")
    assert ok.returncode == 0 and "yousef-docs gh" in ok.stdout and "pull_requests=write" in ok.stdout
    ok = _run(playbook, tmp_path, docs, "agent-gh", "yousef", "pr", "review", "3", "--approve", "-b", "ok")
    assert ok.returncode == 0 and ok.stdout.startswith("agent-gh dry run: yousef gh")  # his own user reviews
    bad = _run(playbook, tmp_path, docs, "agent-gh", "yousef-docs", "pr", "view", "3")
    assert bad.returncode == 1 and "is yousef's" in bad.stderr

    main = _clone(tmp_path, "main")
    ok = _run(playbook, tmp_path, main, "agent-git", "sync", "push", "origin", "main")
    assert ok.returncode == 0 and "sync push on basitasolutions/basita-ops" in ok.stdout
    farm = _clone(tmp_path, "main", url=FARM)
    bad = _run(playbook, tmp_path, farm, "agent-git", "sync", "push", "origin", "main")
    assert bad.returncode == 1 and "pushes only to main of basitasolutions/basita-ops" in bad.stderr

    bad = _run(playbook, tmp_path, main, "agent-gh", "reader", "pr", "comment", "3", "-b", "x")
    assert bad.returncode == 1 and "basita-reader reads only" in bad.stderr
    ok = _run(playbook, tmp_path, main, "agent-gh", "reader", "pr", "checks", "3")
    assert ok.returncode == 0 and "checks=read" in ok.stdout and "statuses=read" in ok.stdout


def test_reader_push_is_refused_in_the_cli(monkeypatch, tmp_path):
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(READY))
    monkeypatch.setattr(identity, "CONFIG", path)
    monkeypatch.setenv("AGENT_GH_DRY_RUN", "1")
    monkeypatch.chdir(_clone(tmp_path, "main"))
    with pytest.raises(IdentityError, match="basita-reader reads only; it never pushes"):
        cli.cmd_push("reader", [])


def test_yousef_commits_as_his_docs_bot_once_it_is_set_up(playbook, tmp_path):
    repo = _clone(tmp_path, "yousef/BASITA-1-plan")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    for root in (_playbook_copy(tmp_path, PENDING, "pending"), playbook):
        subprocess.run(
            [str(root / "scripts" / "agent-git"), "yousef", "commit", "-q", "--allow-empty", "-m", "BASITA-1: x"],
            cwd=repo,
            env=env,
            check=True,
        )
    log = subprocess.run(
        ["git", "log", "--format=%ae|%(trailers:key=Agent,valueonly)"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    # Newest first: with the docs App set up, then with it pending (today's address).
    assert log == ["8005+basita-yousef-docs[bot]@users.noreply.github.com|Yousef", "yousef@basitasolutions.com|Yousef"]


def test_bot_email_of_pending_apps_falls_back(capsys, monkeypatch, tmp_path):
    path = tmp_path / "ids.json"
    path.write_text(json.dumps(PENDING))
    monkeypatch.setattr(identity, "CONFIG", path)
    for name in ("talia", "sync", "reader", "yousef"):
        assert cli.main(["bot-email", name]) == 1
    assert cli.main(["bot-email", "malek"]) == 0
    assert capsys.readouterr().out.strip() == "351705446+basita-malek[bot]@users.noreply.github.com"


# --- the read-only git credential helper ----------------------------------------------------------------------


def _answer(pem, request, config=READY, agent="reader"):
    github = FakeGitHub(
        {("POST", "/app/installations/7007/access_tokens"): (201, {}, {"token": MINTED, "expires_at": "x"})}
    )
    run = keychain({"basita-gh-reader-app": base64.b64encode(pem).decode()})
    return cli.credential_answer(agent, request, config, transport=github, run=run), github


def test_the_credential_helper_mints_a_read_only_token_for_the_one_repository(pem):
    request = {"protocol": "https", "host": "github.com", "path": "basitasolutions/basita-ops.git"}
    answer, github = _answer(pem, request)
    assert answer == f"username=x-access-token\npassword={MINTED}\n"
    ((method, path, auth, body),) = github.calls
    assert method == "POST" and auth.startswith("Bearer ")
    assert body == {"permissions": READER_PERMISSIONS, "repositories": ["basita-ops"]}
    assert all(access == "read" for access in body["permissions"].values())
    answer, _ = _answer(pem, {**request, "path": "basitasolutions/basita-ops"})
    assert MINTED in answer


@pytest.mark.parametrize(
    "request_, answer",
    [
        ({"protocol": "https", "host": "evil.example", "path": "a/b.git"}, ""),
        ({"protocol": "https", "host": "github.com.evil.example", "path": "a/b.git"}, ""),
        ({"protocol": "http", "host": "github.com", "path": "basitasolutions/basita-ops.git"}, ""),
    ],
)
def test_the_credential_helper_answers_only_https_github_com(pem, request_, answer):
    result, github = _answer(pem, request_)
    assert result == answer and github.calls == []


@pytest.mark.parametrize(
    "config, agent, request_, why",
    [
        (READY, "reader", {"protocol": "https", "host": "github.com"}, "useHttpPath"),
        (
            READY,
            "reader",
            {"protocol": "https", "host": "github.com", "path": "someone/x.git"},
            "no listed installation",
        ),
        (READY, "malek", {"protocol": "https", "host": "github.com", "path": "a/b.git"}, "only a read-only App"),
        (READY, "talia", {"protocol": "https", "host": "github.com", "path": "a/b.git"}, "only a read-only App"),
        (PENDING, "reader", {"protocol": "https", "host": "github.com", "path": "a/b.git"}, "not set up yet"),
    ],
)
def test_credential_helper_refusals(pem, config, agent, request_, why):
    with pytest.raises(IdentityError, match=why):
        _answer(pem, request_, config, agent)


def test_the_credential_helper_stores_and_erases_nothing(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: pytest.fail("store and erase read nothing"))
    assert cli.cmd_credential("reader", ["store"]) == 0
    assert cli.cmd_credential("reader", ["erase"]) == 0


def test_git_uses_the_helper_and_gets_nothing_while_the_reader_is_pending(tmp_path):
    """The helper as Talia sets it up at S5, through the real script; pending means no password."""
    helper = f"!{_playbook_copy(tmp_path, PENDING) / 'scripts' / 'agent-gh'} reader credential"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_TERMINAL_PROMPT": "0"}
    fill = subprocess.run(
        [
            "git",
            "-c",
            "credential.https://github.com.helper=",
            "-c",
            f"credential.https://github.com.helper={helper}",
            "-c",
            "credential.https://github.com.useHttpPath=true",
            "credential",
            "fill",
        ],
        input="protocol=https\nhost=github.com\npath=basitasolutions/basita-ops.git\n\n",
        env=env,
        capture_output=True,
        text=True,
    )
    assert "password=" not in fill.stdout and fill.returncode != 0
    assert "basita-reader is not set up yet" in fill.stderr


# --- gh-check ---------------------------------------------------------------------------------------------------


def test_gh_check_shows_pending_apps_as_plain_lines():
    github = FakeGitHub({})
    lines, code = run_check(PENDING, list(NEW), github, no_keychain)
    assert code == 0 and lines == [f"pending {name}: not created yet" for name in NEW]
    assert github.calls == []
    # Not picked up by basita-ops hourly-check.sh, which flags lines that start with FAIL or attention.
    assert not any(line.split()[0] in ("FAIL", "attention") for line in lines)


def _talia_answers(granted):
    return {
        ("GET", "/app"): (200, {}, {"slug": "basita-talia", "id": 9001, "permissions": granted, "events": []}),
        ("GET", "/app/installations?per_page=100&page=1"): (200, {}, []),
    }


def test_gh_check_compares_each_app_with_its_own_permissions(pem):
    config = ready(CONFIG, "talia")
    config["agents"]["talia"]["installations"] = {}
    run = keychain({"basita-gh-talia-app": base64.b64encode(pem).decode()})
    own = config["agents"]["talia"]["permissions"]
    lines, code = run_check(config, ["talia"], FakeGitHub(_talia_answers(own)), run)
    assert "ok App permissions: contents=write issues=read metadata=read pull_requests=write" in "\n".join(lines)
    lines, code = run_check(config, ["talia"], FakeGitHub(_talia_answers(CONFIG["app_permissions"])), run)
    text = "\n".join(lines)
    assert code == 1
    assert "the config expects contents=write issues=read metadata=read pull_requests=write" in text


def test_gh_check_fails_a_read_only_app_with_write_in_the_config_and_a_missing_bot_id():
    config = ready(CONFIG, "reader", "nora")
    config["agents"]["reader"]["permissions"]["pull_requests"] = "write"
    config["agents"]["nora"]["bot_id"] = None
    lines, code = run_check(config, ["reader", "nora"], FakeGitHub({}), no_keychain)
    text = "\n".join(lines)
    assert code == 1 and "FAIL the config gives the read-only basita-reader" in text
    assert "FAIL the config has no bot_id for basita-nora" in text


# --- attribution -----------------------------------------------------------------------------------------------


def test_attribution_maps_the_docs_bot_to_yousef():
    bots, _ = load_bots(CONFIG_FILE, frozenset({"malek", "yousef", "talia"}))
    assert bots["basita-yousef-docs[bot]"] == "yousef"
    assert bots["basita-talia[bot]"] == "talia" and bots["basita-malek[bot]"] == "malek"
    assert "basita-yousef[bot]" not in bots
    assert "basita-yousef-docs[bot]" not in load_bots(CONFIG_FILE, frozenset({"malek"}))[0]


def test_a_docs_bot_commit_needs_yousefs_trailer():
    bots, _ = load_bots(CONFIG_FILE, frozenset({"malek", "yousef"}))
    config = Config(agents=frozenset({"malek", "yousef"}), agent_domain="basitasolutions.com", bots=bots)
    email = "8005+basita-yousef-docs[bot]@users.noreply.github.com"
    pr = PullRequest(title="BASITA-1: plan", branch="yousef/BASITA-1-plan", author_login="basita-yousef-docs[bot]")

    def docs_commit(message):
        return Commit(sha="abcdef1234", author_name="Yousef (Basita AI)", author_email=email, message=message)

    assert check([docs_commit("Plan\n\nAgent: Yousef")], pr, config) == []
    assert [f.rule for f in check([docs_commit("Plan\n\nAgent: Malek")], pr, config)] == ["bot-trailer-mismatch"]
    named = check([docs_commit("Plan\n\nAgent: Yousef")], replace(pr, body="Agent: Malek"), config)
    assert [f.rule for f in named] == ["pr-bot-mismatch"]
