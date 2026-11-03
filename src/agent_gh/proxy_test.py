"""gh-proxy-test: can a cloud session use an App's own token through Anthropic's GitHub proxy? (M0)

    python -m agent_gh.proxy_test <agent> <owner/repo>      (scripts/gh-proxy-test malek basitasolutions/basita-ops)
    python -m agent_gh.proxy_test --whoami                  (step a only, no key needed)

Run in a cloud session whose environment holds BASITA_GH_<AGENT>_KEY (setup S1: basita-proxy-test).
It is read-only except one push of one empty commit to the throwaway branch
`<prefix>BASITA-153-proxy-test` (for malek: malek/BASITA-153-proxy-test), which it never deletes. It
prints only logins, HTTP status codes and yes/no answers, never a token or a key. Steps:

  a. who the proxy attaches to a request with no Authorization (GET /user)
  b. the App's JWT from the cloud key (GET /app): does a Bearer JWT pass through?
  c. an installation token for that one repository (contents write, metadata read)
  d. REST with it: GET /installation/repositories answers only an installation token; GET /user answers
     only a user token (it would mean the proxy put a user's token in its place); GET /repos/{repo}
  e. the same through `gh api`, with GH_TOKEN in gh's environment
  f. GraphQL with it (expected: blocked)
  g. `git push` of one empty commit, the token as http.extraHeader from the environment; then who
     pushed it (activity API) and the commit's author
  h. the token revoked

The last lines are `RESULT: push=... rest=... graphql=...` (app = the App's own token went through,
ahmed = ahmedoraby-eng's did, other:<login>, refused, unknown) and the one question a script can't
answer. Exit 0 when it ran, 1 when it couldn't start, 2 on bad arguments.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING

from .__main__ import CLEARED, CLEARED_PREFIXES
from .identity import (
    API,
    PUSH,
    IdentityError,
    agent_entry,
    app_jwt,
    bot_email,
    bot_login,
    cloud_key_variable,
    env_private_key,
    installation_for,
    load_config,
    never_in_cloud,
    require_ready,
    split_repo,
    urllib_transport,
)
from .policy import plan_push

if TYPE_CHECKING:
    from collections.abc import Callable

    from .identity import Transport

AHMED = "ahmedoraby-eng"
USAGE = "usage: gh-proxy-test <agent> <owner/repo> | gh-proxy-test --whoami"
HUMAN = (
    "HUMAN: one answer needs a person: which credential the proxy attaches when both the Claude GitHub App "
    "and a /web-setup token are connected. Run scripts/gh-proxy-test --whoami before and after /web-setup "
    "and compare the two logins of step a."
)


def call(transport: Transport, method: str, path: str, auth: str, body: dict | None = None) -> tuple[str, object]:
    """One REST call that never raises: (status, JSON). The status is "unreachable" when no answer came."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "basita-m0"}
    if auth:
        headers["Authorization"] = auth
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        status, _, text = transport(method, f"{API}{path}", headers, data)
    except IdentityError:
        return "unreachable", {}
    except OSError as error:  # TLS and socket errors that urllib doesn't wrap
        return f"unreachable ({type(error).__name__})", {}
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    return str(status), payload


def _login(payload: object) -> str | None:
    return payload.get("login") if isinstance(payload, dict) else None


def whoami(transport: Transport) -> str:
    status, payload = call(transport, "GET", "/user", "")
    login = _login(payload) if status == "200" else None
    return f"a. no Authorization: GET /user -> {status}" + (f", login {login}" if login else "")


def clean_env(environ: dict | None = None) -> dict:
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if k not in CLEARED and not k.startswith(CLEARED_PREFIXES)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


class ProxyTest:
    def __init__(
        self,
        agent: str,
        repo: str,
        config: dict,
        transport: Transport = urllib_transport,
        run: Callable = subprocess.run,
        environ: dict | None = None,
        openssl: str = "openssl",
        sleep: Callable[[float], None] = time.sleep,
        workdir: Callable[[], str] = lambda: tempfile.mkdtemp(prefix="gh-proxy-test-"),
    ):
        self.agent = agent.lower()
        self.repo = repo
        self.config = config
        self.transport = transport
        self.run = run
        self.environ = os.environ if environ is None else environ
        self.openssl = openssl
        self.sleep = sleep
        self.workdir = workdir
        self.lines: list[str] = []
        self.entry = agent_entry(config, self.agent)
        self.branch = f"{self.entry.get('branch_prefix') or ''}BASITA-153-proxy-test"

    def say(self, line: str) -> None:
        self.lines.append(line)
        print(line, flush=True)

    def who(self, login: str | None) -> str:
        if not login:
            return "unknown"
        if login == bot_login(self.entry):
            return "app"
        return "ahmed" if login == AHMED else f"other:{login}"

    def prepare(self) -> None:
        """Every refusal before anything goes to GitHub: the App, the repository and the branch."""
        if self.entry.get("kind") != "app":
            raise IdentityError(f"refused: {self.agent} is not an App; the proxy test runs with an App's key")
        if never_in_cloud(self.agent, self.entry):
            raise IdentityError(f"refused: {self.entry['slug']} never has a key in the cloud (plan 10.8)")
        require_ready(self.entry)
        owner, _ = split_repo(self.repo)
        self.installation = installation_for(self.entry, owner)
        url = f"https://github.com/{self.repo}.git"
        plan_push([url, f"HEAD:refs/heads/{self.branch}"], None, url, self.config, self.entry)  # prefix policy
        self.pem = env_private_key(self.agent, self.entry, self.environ)

    def main(self) -> int:
        if (self.environ.get("CLAUDE_CODE_REMOTE") or "").lower() != "true":
            self.say(
                "note: CLAUDE_CODE_REMOTE is not true, so this is not a cloud session; the answers are this machine's"
            )
        self.say(whoami(self.transport))
        try:
            self.prepare()
        except IdentityError as error:
            self.say(f"b. stopped before any call with the key: {error}")
            self.say("RESULT: push=refused rest=refused graphql=unknown")
            self.say(HUMAN)
            return 1
        token = None
        rest = graphql = push = "refused"
        try:
            jwt = app_jwt(self.entry, self.pem, openssl=self.openssl)
            del self.pem
            status, app = call(self.transport, "GET", "/app", f"Bearer {jwt}")
            slug = app.get("slug") if isinstance(app, dict) else None
            passed = "yes" if status == "200" and slug == self.entry["slug"] else "no"
            self.say(
                f"b. App JWT from {cloud_key_variable(self.agent)}: GET /app -> {status}"
                + (f", slug {slug}" if slug else "")
                + f"; the JWT passed through: {passed}"
            )
            _, name = split_repo(self.repo)
            status, minted = call(
                self.transport,
                "POST",
                f"/app/installations/{self.installation}/access_tokens",
                f"Bearer {jwt}",
                {"permissions": PUSH, "repositories": [name]},
            )
            token = minted.get("token") if isinstance(minted, dict) else None
            self.say(
                f"c. installation token for {self.repo} (contents write, metadata read): "
                + ("ok" if token else f"none ({status})")
            )
            if token:
                rest = self.rest(token)
                self.gh_api(token)
                graphql = self.graphql(token)
                push = self.push(token)
            else:
                self.say("d-g. skipped: no installation token")
        finally:
            if token:
                status, _ = call(self.transport, "DELETE", "/installation/token", f"token {token}")
                self.say(f"h. token revoked: {'yes' if status == '204' else 'no'} ({status})")
                token = None
        self.say(f"RESULT: push={push} rest={rest} graphql={graphql}")
        self.say(HUMAN)
        return 0

    def rest(self, token: str) -> str:
        auth = f"token {token}"
        repos, _ = call(self.transport, "GET", "/installation/repositories?per_page=1", auth)
        user, payload = call(self.transport, "GET", "/user", auth)
        login = _login(payload) if user == "200" else None
        repo, _ = call(self.transport, "GET", f"/repos/{self.repo}", auth)
        self.say(
            f"d. REST with the token: GET /installation/repositories -> {repos} "
            f"(the App's token passed through: {'yes' if repos == '200' else 'no'}); "
            f"GET /user -> {user}" + (f", login {login}" if login else "") + f"; GET /repos/{self.repo} -> {repo}"
        )
        if repos == "200":
            return "app"
        return self.who(login) if login else "refused"

    def gh_api(self, token: str) -> None:
        env = clean_env(self.environ)
        env.update(
            {"GH_TOKEN": token, "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
        )
        try:
            result = self.run(["gh", "api", "-i", "installation/repositories"], env=env, capture_output=True, text=True)
        except FileNotFoundError:
            self.say("e. gh api: gh is not installed here")
            return
        found = re.match(r"HTTP/\S+ (\d{3})", result.stdout or "") or re.search(
            r"\(HTTP (\d{3})\)", result.stderr or ""
        )
        status = found.group(1) if found else f"no status (exit {result.returncode})"
        self.say(f"e. gh api with GH_TOKEN: installation/repositories -> {status}")

    def graphql(self, token: str) -> str:
        status, payload = call(self.transport, "POST", "/graphql", f"bearer {token}", {"query": "{viewer{login}}"})
        data = payload.get("data") if isinstance(payload, dict) else None
        login = ((data or {}).get("viewer") or {}).get("login") if isinstance(data, dict) else None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        detail = f", viewer {login}" if login else (", GraphQL errors" if errors else "")
        self.say(f"f. GraphQL with the token: POST /graphql -> {status}{detail}")
        if status == "200" and login:
            return self.who(login)
        return "errors" if status == "200" and errors else "blocked"

    def git(self, folder: str, *args: str, env: dict | None = None, stdin: str | None = None):
        command = ["git", "-C", folder, "-c", "credential.helper=", "-c", "push.recurseSubmodules=no", *args]
        return self.run(command, env=env, input=stdin, capture_output=True, text=True)

    def push(self, token: str) -> str:
        auth = f"token {token}"
        url = f"https://github.com/{self.repo}.git"
        folder = self.workdir()
        env = clean_env(self.environ)
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        # One URL only, so the header isn't scoped: it then also reaches a proxy that git rewrites github.com to.
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader"})
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
        env.update(
            {
                "GIT_AUTHOR_NAME": f"{self.agent.capitalize()} (Basita AI)",
                "GIT_AUTHOR_EMAIL": bot_email(self.entry),
                "GIT_COMMITTER_NAME": f"{self.agent.capitalize()} (Basita AI)",
                "GIT_COMMITTER_EMAIL": bot_email(self.entry),
            }
        )
        self.run(["git", "init", "--bare", "-q", folder], capture_output=True, text=True, env=clean_env(self.environ))
        rewrites = self.git(folder, "config", "--get-regexp", r"^url\..*\.insteadof$")
        rewritten = any(
            line.split(" ", 1)[-1].startswith(("https://github.com", "github.com"))
            for line in (rewrites.stdout or "").splitlines()
        )
        tree = self.git(folder, "hash-object", "-t", "tree", "-w", "--stdin", stdin="").stdout.strip()
        parents: list[str] = []
        status, ref = call(self.transport, "GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}", auth)
        if status == "200":  # a run before this one: build on its commit, so the push is a fast-forward
            fetched = self.git(folder, "fetch", "-q", "--depth=1", url, f"refs/heads/{self.branch}", env=env)
            if fetched.returncode != 0:
                self.say(f"g. git push: refused (fetching the earlier test commit failed, exit {fetched.returncode})")
                return "refused"
            parents = ["-p", self.git(folder, "rev-parse", "FETCH_HEAD").stdout.strip()]
        message = f"BASITA-153: proxy test (M0), an empty commit\n\nAgent: {self.agent.capitalize()}\n"
        sha = self.git(folder, "commit-tree", tree, *parents, "-F", "-", env=env, stdin=message).stdout.strip()
        pushed = self.git(folder, "push", "-q", url, f"{sha}:refs/heads/{self.branch}", env=env)
        line = (
            f"g. git push of an empty commit to {self.branch} (git rewrites github.com: {'yes' if rewritten else 'no'})"
        )
        if pushed.returncode != 0:
            denied = re.search(r"denied to ([A-Za-z0-9\[\]-]+)", pushed.stderr or "")
            self.say(
                f"{line}: refused (exit {pushed.returncode})" + (f", denied to {denied.group(1)}" if denied else "")
            )
            return "refused"
        actor = None
        for attempt in range(3):  # the activity API can lag a push by a few seconds
            status, activity = call(
                self.transport, "GET", f"/repos/{self.repo}/activity?ref=refs/heads/{self.branch}&per_page=1", auth
            )
            if status == "200" and isinstance(activity, list) and activity and activity[0].get("after") == sha:
                actor = (activity[0].get("actor") or {}).get("login")
                break
            if attempt < 2:
                self.sleep(3)
        _, commit = call(self.transport, "GET", f"/repos/{self.repo}/commits/{sha}", auth)
        author = ((commit.get("author") or {}).get("login") if isinstance(commit, dict) else None) or "unknown"
        self.say(
            f"{line}: pushed; pushed by {actor or f'unknown (activity API {status})'}; commit author {author}; "
            "branch left for Malek to report; Ahmed may delete it"
        )
        return self.who(actor)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--whoami"]:
        print(whoami(urllib_transport))
        print(HUMAN)
        return 0
    if len(argv) != 2 or argv[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        return ProxyTest(argv[0], argv[1], load_config()).main()
    except IdentityError as error:
        print(f"gh-proxy-test: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
