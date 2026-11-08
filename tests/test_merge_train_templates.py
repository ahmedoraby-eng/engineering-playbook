"""The merge train's workflow files ship as templates (no agent App can push .github/workflows, D4).
These tests pin the properties that keep them safe to install."""

from __future__ import annotations

import re
from pathlib import Path

from merge_train import parse

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "merge-train"
REUSABLE = (TEMPLATES / "reusable-merge-train.yml").read_text()
CALLER = (TEMPLATES / "merge-train.yml").read_text()
SKIP = (
    "((github.event_name == 'pull_request_target' && github.event.label.name != 'reviewed-by-ahmed') || "
    "(github.event_name == 'workflow_run' && github.event.workflow_run.head_branch == 'main'))"
)


def step_run_blocks(text: str) -> list[str]:
    return re.findall(r"\n\s+run: \|\n((?:\s{10,}.*\n?)+)", text)


def test_the_app_token_is_scoped_to_contents_and_pull_requests_only():
    scopes = re.findall(r"^\s+permission-([a-z-]+): (\w+)$", REUSABLE, re.M)
    assert sorted(scopes) == [("contents", "write"), ("pull-requests", "write")]
    assert "owner:" not in REUSABLE and "repositories:" not in REUSABLE, "defaults to this repository only"


def test_reads_use_the_github_token_with_read_permissions_only():
    block = re.search(r"^permissions:\n((?:  .*\n)+)", REUSABLE, re.M).group(1)
    assert "write" not in block
    assert "GH_READ_TOKEN: ${{ github.token }}" in REUSABLE
    assert "GH_WRITE_TOKEN: ${{ steps.app.outputs.token }}" in REUSABLE


def test_the_train_has_no_way_to_merge():
    for text in (REUSABLE, CALLER):
        assert not re.search(r"pulls/\S*/merge\b|gh pr merge|auto-merge|merge_method", text)


def test_nothing_from_the_event_is_interpolated_into_a_script():
    blocks = step_run_blocks(REUSABLE)
    assert blocks, "the run block is found"
    assert not any("${{" in block for block in blocks)


def test_third_party_code_is_pinned_to_a_major_version():
    for use in re.findall(r"uses: (\S+)", REUSABLE):
        assert re.search(r"@v\d+$|@main$", use), use


def test_it_runs_slim_with_a_timeout_and_only_what_the_adr_and_the_green_signal_need():
    assert "runs-on: ubuntu-slim" in REUSABLE and "timeout-minutes:" in REUSABLE
    on = re.search(r"^on:\n((?:  .*\n)+)", CALLER, re.M).group(1)
    assert re.findall(r"^  (\w+):", on, re.M) == ["push", "workflow_dispatch", "workflow_run", "pull_request_target"]
    assert "branches: [main]" in on


def test_a_run_that_would_be_skipped_cannot_replace_a_real_one_in_the_queue():
    assert CALLER.count(SKIP) == 2, "the concurrency group and the job condition use the same expression"
    assert "cancel-in-progress: false" in CALLER
    assert "-skip-{0}" in CALLER


def test_the_caller_passes_the_app_secrets_and_asks_for_read_permissions_only():
    assert "app-id: ${{ secrets.TRAIN_APP_ID }}" in CALLER
    assert "app-private-key: ${{ secrets.TRAIN_APP_PRIVATE_KEY }}" in CALLER
    block = re.search(r"^permissions:\n((?:  .*\n)+)", CALLER, re.M).group(1)
    assert "write" not in block


def test_the_starter_order_file_is_valid_and_empty():
    cfg = parse((TEMPLATES / "merge-train.json").read_text())
    assert cfg.order == ()


def test_the_train_app_is_fenced_in_the_identity_config():
    import json

    config = json.loads((TEMPLATES.parent.parent / "config" / "github-identities.json").read_text())
    train = config["agents"]["train"]
    assert train["kind"] == "app" and train["slug"] == "basita-train"
    assert train["permissions"] == {"contents": "write", "metadata": "read", "pull_requests": "write"}
    assert train["branch_prefix"] == "train/", "if run as an identity it could push train/* only, never main"
    assert not train.get("updates_main") and not train.get("read_only")
    assert train["app_id"] is None, "pending until Ahmed creates it (scripts/gh-app-new train)"
