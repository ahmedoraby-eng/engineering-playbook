"""What changes for agent-gh and agent-git push in a cloud session (plan 10.8, BASITA-163).

In an Anthropic-hosted cloud session every GitHub request goes through Anthropic's GitHub proxy, which
blocks GraphQL whatever token is used. Claude Code sets CLAUDE_CODE_REMOTE=true there. A session also
counts as a cloud one when an App key would come from `BASITA_GH_<AGENT>_KEY`: no Keychain (no
`security`) and such a variable set (Yousef's review of #37), so the gates below don't rest on one
variable alone.

- **Key.** There is no Keychain, so an App's key comes from `BASITA_GH_<AGENT>_KEY` (identity.py).
- **REST mode.** `gh pr ...` speaks GraphQL, so agent-gh runs `pr create|view|comment|checks` through
  REST itself (rest.py), after the policy approved the original command. It is on in a cloud session,
  and AGENT_GH_REST=1 or 0 turns it on or off anywhere (to try it on the Mac, or to turn it off).
- **Writes.** Until M0 shows a REST call carries the App's own token, a gh write (pull request,
  comment) from a cloud session is refused as a push is; reads work. AGENT_GH_CLOUD_WRITES=1 turns
  writes on once M0 says rest=app.
- **Push.** Whether `git push` carries the App's own token through the proxy is what the proxy test
  (M0, BASITA-156) answers. Until it says so, a push from a cloud session is refused, because it could
  go out as the GitHub account connected to claude.ai. AGENT_GIT_CLOUD_PUSH=git turns it on once M0
  shows push=app. The token then goes as an `http.extraHeader` from the environment (never argv), so
  git sends it on the first request instead of waiting for a 401 that the proxy may never give.
"""

from __future__ import annotations

import base64
import os
import re
import shutil

from .identity import IdentityError

REMOTE = "CLAUDE_CODE_REMOTE"
REST_SWITCH = "AGENT_GH_REST"
PUSH_SWITCH = "AGENT_GIT_CLOUD_PUSH"
WRITE_SWITCH = "AGENT_GH_CLOUD_WRITES"
ON = ("1", "true", "on", "yes")
OFF = ("0", "false", "off", "no")


KEY_VARIABLE = re.compile(r"^BASITA_GH_[A-Z0-9_]+_KEY$")


def key_from_environment(environ: dict | None = None) -> bool:
    """An App key would come from the environment: a BASITA_GH_<AGENT>_KEY is set and there is no Keychain
    (identity.py reads the variable only then; on the Mac the Keychain always wins)."""
    environ = os.environ if environ is None else environ
    if not any(KEY_VARIABLE.match(name) and (value or "").strip() for name, value in environ.items()):
        return False
    return shutil.which("security", path=environ.get("PATH", os.defpath)) is None


def in_cloud(environ: dict | None = None) -> bool:
    environ = os.environ if environ is None else environ
    return (environ.get(REMOTE) or "").strip().lower() == "true" or key_from_environment(environ)


def rest_mode(environ: dict | None = None) -> bool:
    """REST instead of gh's GraphQL: AGENT_GH_REST=1|0 decides; otherwise on in a cloud session."""
    environ = os.environ if environ is None else environ
    switch = (environ.get(REST_SWITCH) or "").strip().lower()
    if switch in ON:
        return True
    if switch in OFF:
        return False
    return in_cloud(environ)


def require_cloud_write(permissions: dict, environ: dict | None = None) -> None:
    """Nothing on the Mac, and nothing for a read. In a cloud session a gh write (a pull request, a
    comment) waits for M0 as a push does: if the proxy put the claude.ai account's token in place of
    the App's, it would be written as that account. AGENT_GH_CLOUD_WRITES=1 once M0 says rest=app."""
    environ = os.environ if environ is None else environ
    if not in_cloud(environ) or all(access != "write" for access in permissions.values()):
        return
    if (environ.get(WRITE_SWITCH) or "").strip().lower() in ON:
        return
    raise IdentityError(
        "refused: a GitHub write from a cloud session waits for the proxy test (M0, BASITA-156). Until it "
        "shows that a REST call carries the App's own token, it could be written as the account connected "
        f"to claude.ai. Reads work; {WRITE_SWITCH}=1 turns writes on once M0 says rest=app"
    )


def require_cloud_push(environ: dict | None = None) -> None:
    """Nothing on the Mac. In a cloud session, a push waits for M0's answer (D19c)."""
    environ = os.environ if environ is None else environ
    if not in_cloud(environ):
        return
    mode = (environ.get(PUSH_SWITCH) or "").strip().lower()
    if mode == "git":
        return
    if mode == "git-data":
        raise IdentityError(
            "refused: the push through the Git Data API is not built yet (BASITA-163, 'After M0'); "
            "cloud work waits for the Mac to push (D19c)"
        )
    raise IdentityError(
        "refused: a push from a cloud session waits for the proxy test (M0, BASITA-156). Until it shows "
        f"that a push carries the App's own token, it could go out as the account connected to claude.ai. "
        f"The Mac pushes when it wakes (D19c); {PUSH_SWITCH}=git turns it on once M0 says push=app"
    )


def push_env(token: str, environ: dict | None = None) -> dict:
    """In a cloud session, the token as git's http.extraHeader for https://github.com/, through the
    environment (GIT_CONFIG_*), so it is in no process's arguments. Nothing on the Mac."""
    if not in_cloud(environ):
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }
