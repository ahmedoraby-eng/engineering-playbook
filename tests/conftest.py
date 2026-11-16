import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_github_pause(tmp_path_factory, monkeypatch):
    """BASITA-233 (Yousef P1): the suite never reads the real ~/.config/basita/github-writes-paused.

    Every test sees an absent pause file unless it sets GITHUB_WRITES_PAUSE_FILE itself; subprocess tests that build
    their own environment get a temp HOME, so the default path is absent there too."""
    monkeypatch.setenv("GITHUB_WRITES_PAUSE_FILE", str(tmp_path_factory.mktemp("pause") / "github-writes-paused.absent"))
