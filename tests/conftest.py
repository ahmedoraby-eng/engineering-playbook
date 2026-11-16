import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_github_pause(tmp_path_factory, monkeypatch):
    """BASITA-233 (Yousef P1/P1b): the suite never reads the real ~/.config/basita/github-writes-paused.

    In process, the module's default path is patched to an absent temp file and the override is unset (the override
    can only ADD a pause, so the env alone can't isolate). Subprocess tests build their own environment with a temp
    HOME, so the default path is absent there too."""
    from agent_gh import __main__ as agent_gh_cli

    absent = tmp_path_factory.mktemp("pause") / "github-writes-paused.absent"
    monkeypatch.setattr(agent_gh_cli, "PAUSE_FILE", str(absent))
    monkeypatch.delenv("GITHUB_WRITES_PAUSE_FILE", raising=False)
