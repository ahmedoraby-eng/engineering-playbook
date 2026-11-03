"""gh-cloud-key: an App's cloud key as one base64 line on the clipboard, for Ahmed's setup S1 and S8.

    python -m agent_gh.cloud_key <agent> --file ~/Downloads/basita-<agent>.<date>.private-key.pem

Runs on Ahmed's Mac. The .pem is a second private key Ahmed generated for the App in its GitHub
settings, so the cloud copy can be revoked alone (plan 10.8, D19a). The script checks that the file is
a private key and that it is this App's (it signs a JWT and reads `GET /app`), and that it isn't the
Mac's own key. Then it puts one base64 line on the clipboard through pbcopy's stdin and prints only its
length. It never writes the key to disk or to the screen, and never stores it in the Keychain: the
Keychain holds the Mac's key, and this one goes into the claude.ai environment as BASITA_GH_<AGENT>_KEY.
sync and reader never get a cloud key.
"""

from __future__ import annotations

import base64
import hmac
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .identity import (
    IdentityError,
    agent_entry,
    api,
    app_jwt,
    cloud_key_variable,
    load_config,
    never_in_cloud,
    private_key,
    require_ready,
    urllib_transport,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .identity import Transport

USAGE = "usage: gh-cloud-key <agent> --file <path to the .pem>"


def pbcopy(text: str) -> None:
    """The clipboard, from stdin, so the key is in no process's arguments."""
    try:
        result = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True)
    except FileNotFoundError:
        raise IdentityError("no pbcopy here: run gh-cloud-key on the Mac") from None
    if result.returncode != 0:
        raise IdentityError("pbcopy refused the key: nothing copied")


def read_pem(file: str) -> bytes:
    try:
        raw = Path(file).expanduser().read_bytes().strip()
    except OSError as error:
        raise IdentityError(f"cannot read {file}: {error.strerror}") from None
    if not re.match(rb"-----BEGIN (RSA )?PRIVATE KEY-----", raw) or not re.search(
        rb"-----END (RSA )?PRIVATE KEY-----$", raw
    ):
        raise IdentityError(f"{file} is not a private key: nothing copied")
    return raw + b"\n"


def cloud_key(
    agent: str,
    file: str,
    config: dict,
    transport: Transport = urllib_transport,
    copy: Callable[[str], None] = pbcopy,
    run: Callable = subprocess.run,
    openssl: str = "openssl",
) -> str:
    """Checks the key and copies it. Returns the one line to print, which holds no part of the key."""
    agent = agent.lower()
    entry = agent_entry(config, agent)
    if entry.get("kind") != "app":
        raise IdentityError(f"refused: {entry.get('login', agent)} is a user; his tokens stay in the Mac's Keychain")
    if never_in_cloud(agent, entry):
        raise IdentityError(f"refused: {entry['slug']} never has a key in the cloud (plan 10.8)")
    require_ready(entry)
    pem = read_pem(file)
    _, app = api(transport, "GET", "/app", f"Bearer {app_jwt(entry, pem, openssl=openssl)}")
    if app.get("slug") != entry["slug"] or int(app.get("id") or 0) != int(entry["app_id"]):
        raise IdentityError(
            f"refused: the key belongs to {app.get('slug')} (id {app.get('id')}), not {entry['slug']}: nothing copied"
        )
    try:
        mac_key = private_key(entry["keychain"], run)
    except IdentityError:  # no Keychain, or no Mac key stored yet: nothing to compare with
        mac_key = b""
    if mac_key and hmac.compare_digest(mac_key, pem):
        raise IdentityError(
            f"refused: this is the Mac's key of {entry['slug']}; generate a second private key for the cloud "
            "in the App's settings, so it can be revoked alone: nothing copied"
        )
    line = base64.b64encode(pem).decode("ascii")
    copy(line)
    size = len(line)
    del line, pem, mac_key
    return (
        f"copied the cloud key of {entry['slug']} ({size} chars); paste it into the claude.ai environment as "
        f"{cloud_key_variable(agent)}, then clear the clipboard (pbcopy </dev/null) and move the .pem to the Trash"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or argv[1] != "--file":
        print(USAGE, file=sys.stderr)
        return 2
    try:
        print(cloud_key(argv[0], argv[2], load_config()))
    except IdentityError as error:
        print(f"gh-cloud-key: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
