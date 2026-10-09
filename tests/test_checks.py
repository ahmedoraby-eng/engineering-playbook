import pytest

from ai_guard.checks import ai_disclosure, run_all
from ai_guard.diff import parse_diff
from ai_guard.policy import Policy
from helpers import FAKE_AWS_KEY, FAKE_GH_TOKEN, diff_for, pr_body

POLICY = Policy()


def rules(findings, severity="error"):
    return sorted(f.rule for f in findings if f.severity == severity)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (pr_body(no_ai=True), False),
        (pr_body(assisted=True), True),
        (pr_body(), None),
        (pr_body(no_ai=True, assisted=True), None),
        ("", None),
    ],
)
def test_reads_the_ai_disclosure_checkboxes(body, expected):
    assert ai_disclosure(body) is expected


def test_clean_human_change_passes():
    changes = parse_diff(diff_for({"src/app.py": ["x = 1"]}))
    assert run_all(changes, pr_body(no_ai=True), set(), POLICY) == []


def test_missing_disclosure_fails():
    changes = parse_diff(diff_for({"README.md": ["hello"]}))
    assert rules(run_all(changes, pr_body(), set(), POLICY)) == ["ai-disclosure"]


@pytest.mark.parametrize(
    "line",
    [
        f'aws_key = "{FAKE_AWS_KEY}"',
        f"token: {FAKE_GH_TOKEN}",
        "-----BEGIN RSA " + "PRIVATE KEY-----",
        "API_" + 'KEY = "abcdef0123456789xyz"',
        "client = Client(key='sk-" + "z" * 24 + "')",
    ],
)
def test_secrets_in_added_lines_fail_with_location(line):
    changes = parse_diff(diff_for({"src/config.py": ["ok = True", line]}))
    findings = run_all(changes, pr_body(no_ai=True), set(), POLICY)
    secret = [f for f in findings if f.rule == "no-secrets"]
    assert len(secret) == 1
    assert (secret[0].path, secret[0].line) == ("src/config.py", 2)


def test_secret_lines_can_be_explicitly_allowed():
    line = f'EXAMPLE = "{FAKE_AWS_KEY}"  # ai-guard: allow (documented example key)'
    changes = parse_diff(diff_for({"docs/setup.py": [line]}))
    assert run_all(changes, pr_body(no_ai=True), set(), POLICY) == []


def test_ignored_paths_are_not_scanned():
    policy = Policy(secret_scan_ignore=("tests/fixtures/*",))
    changes = parse_diff(diff_for({"tests/fixtures/leak.txt": [f"key={FAKE_AWS_KEY}"]}))
    assert rules(run_all(changes, pr_body(no_ai=True), set(), policy)) == []


def test_ai_source_change_without_tests_fails():
    changes = parse_diff(diff_for({"src/billing.py": ["def charge(): ..."]}))
    assert rules(run_all(changes, pr_body(assisted=True), set(), POLICY)) == ["tests-with-ai-code"]


def test_ai_source_change_with_tests_passes():
    changes = parse_diff(
        diff_for({"src/billing.py": ["def charge(): ..."], "tests/test_billing.py": ["def test(): ..."]})
    )
    assert run_all(changes, pr_body(assisted=True), set(), POLICY) == []


def test_human_source_change_without_tests_is_not_blocked():
    changes = parse_diff(diff_for({"src/billing.py": ["def charge(): ..."]}))
    assert run_all(changes, pr_body(no_ai=True), set(), POLICY) == []


def test_ai_change_to_sensitive_area_needs_human_review_label():
    files = {"src/auth/session.py": ["TTL = 30"], "tests/test_session.py": ["def test(): ..."]}
    changes = parse_diff(diff_for(files))
    assert rules(run_all(changes, pr_body(assisted=True), set(), POLICY)) == ["protected-path"]
    assert run_all(changes, pr_body(assisted=True), {"human-reviewed"}, POLICY) == []


def test_large_ai_change_warns_but_does_not_fail():
    files = {"src/big.py": [f"x{i} = {i}" for i in range(450)], "tests/test_big.py": ["def test(): ..."]}
    findings = run_all(parse_diff(diff_for(files)), pr_body(assisted=True), set(), POLICY)
    assert rules(findings) == []
    assert rules(findings, "warning") == ["reviewable-size"]
