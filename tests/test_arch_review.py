import json
import subprocess

import pytest

from arch_review import ChangedFile, Review, Rules, classify, decide
from arch_review.__main__ import FetchError, GitHub, main
from arch_review.review import change_set, latest_review
from arch_review.rules import RulesError, commit_author

OWNER, REVIEWER, AGENT = "owner-login", "arch-bot", "agent-app[bot]"
HEAD, OLD, BASE = "h" * 40, "o" * 40, "b" * 40
RULES_JSON = {
    "owner": OWNER,
    "critical_paths": ["^\\.github/", "^src/calc/", "(^|/)CLAUDE\\.md$", "package\\.json$"],
    "size_exclude": ["^(docs|tests)/", "\\.md$"],
    "architecture_review": {
        "reviewer": REVIEWER,
        "docs_paths": ["\\.md$", "^docs/"],
        "test_paths": ["^tests/", "\\.test\\.ts$"],
        "large_lines": 400,
    },
}
RULES = Rules.from_json(RULES_JSON)


def f(name, changes=10, **kw):
    return ChangedFile(filename=name, changes=changes, **kw)


def cls(files, authors=(AGENT,), labels=(), waived_by="", changed=None, rules=RULES):
    return classify(rules, list(authors), files, set(labels), waived_by, changed)


# --- rules file ---------------------------------------------------------------------------------


def test_rules_defaults_exempt_the_owner_and_effort_by_class():
    assert RULES.exempt_authors == frozenset({OWNER})
    assert RULES.effort == {"critical": "max", "large": "xhigh", "normal": "high"}
    assert RULES.skip_test_only is False
    assert (RULES.waive_label, RULES.needs_label) == ("review-waived", "needs-yousef")


@pytest.mark.parametrize(
    "change",
    [
        {"owner": ""},
        {"architecture_review": None},
        {"critical_paths": []},
        {"critical_paths": ["("]},
        {"size_exclude": "x"},  # a string is accepted as one pattern, so this one is valid
    ],
)
def test_bad_rules_are_refused(change):
    data = {**RULES_JSON, **change}
    if change == {"size_exclude": "x"}:
        assert Rules.from_json(data).size_exclude.pattern == "(?:x)"
        return
    with pytest.raises(RulesError):
        Rules.from_json(data)


@pytest.mark.parametrize("large", [0, True, "400"])
def test_large_lines_must_be_a_positive_whole_number(large):
    data = {**RULES_JSON, "architecture_review": {**RULES_JSON["architecture_review"], "large_lines": large}}
    with pytest.raises(RulesError):
        Rules.from_json(data)


def test_rules_must_be_an_object_with_a_reviewer():
    with pytest.raises(RulesError):
        Rules.from_json([])
    with pytest.raises(RulesError):
        Rules.from_json({**RULES_JSON, "architecture_review": {**RULES_JSON["architecture_review"], "reviewer": ""}})


def test_changed_file_from_github_counts_additions_and_deletions_when_changes_is_missing():
    item = {"filename": "b.ts", "additions": 3, "deletions": 2, "status": "renamed", "previous_filename": "a.ts"}
    file = ChangedFile.from_github(item)
    assert (file.changes, file.names()) == (5, ("b.ts", "a.ts"))


# --- classification -------------------------------------------------------------------------------


def test_owner_only_commits_need_no_review_even_on_critical_paths():
    c = cls([f(".github/workflows/ci.yml")], authors=[OWNER])
    assert (c.cls, c.effort, c.required) == ("not-required", "", False)


def test_needs_label_asks_for_a_review_of_the_owners_own_pr():
    c = cls([f("src/app.ts")], authors=[OWNER], labels=["needs-yousef"])
    assert (c.cls, c.effort) == ("normal", "high")
    assert "labelled 'needs-yousef'" in c.reasons


# --- the minor label (BASITA-221) -----------------------------------------------------------------


def test_rules_default_minor_label_and_its_validation():
    assert RULES.minor_label == "minor"
    arch = {**RULES_JSON["architecture_review"], "minor_label": "polish"}
    assert Rules.from_json({**RULES_JSON, "architecture_review": arch}).minor_label == "polish"
    for bad in ("two words", 7, None):
        with pytest.raises(RulesError, match="minor_label"):
            Rules.from_json(
                {**RULES_JSON, "architecture_review": {**RULES_JSON["architecture_review"], "minor_label": bad}}
            )


def test_a_minor_pr_skips_the_architect_with_a_note():
    c = cls([f("src/app.css", 20), f("src/Title.tsx", 4)], labels=["minor"])
    assert (c.cls, c.effort, c.required) == ("not-required", "", False)
    assert c.reasons == ("labelled 'minor': no architecture review",)


def test_the_same_pr_without_the_label_is_reviewed():
    assert cls([f("src/app.css", 20)]).cls == "normal"


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "src/calc/rate.ts", "tools/CLAUDE.md", "package.json"])
def test_logic_data_security_and_ci_paths_are_never_minor(path):
    c = cls([f("src/app.css"), f(path)], labels=["minor"])
    assert (c.cls, c.effort) == ("critical", "max")
    assert "'minor' ignored: a critical path is never minor" in c.reasons


def test_a_file_renamed_off_a_critical_path_is_not_minor():
    c = cls([f("src/other/rate.ts", previous_filename="src/calc/rate.ts")], labels=["minor"])
    assert c.cls == "critical"


def test_a_cut_short_file_list_is_not_minor():
    c = cls([f("src/app.css")], labels=["minor"], changed=3001)
    assert c.cls == "critical"
    assert "'minor' ignored: the file list is incomplete" in c.reasons


def test_a_large_pr_is_not_minor_because_anyone_may_add_the_label():
    c = cls([f("src/app.css", 401)], labels=["minor"])
    assert (c.cls, c.effort) == ("large", "xhigh")
    assert any(r.startswith("'minor' ignored: 401 changed lines") for r in c.reasons)
    assert cls([f("src/app.css", 400), f("tests/a.test.ts", 5000)], labels=["minor"]).cls == "not-required"


def test_needs_label_beats_minor():
    c = cls([f("src/app.css")], labels=["minor", "needs-yousef"])
    assert c.cls == "normal"
    assert "labelled 'needs-yousef'" in c.reasons
    assert not any("minor" in r for r in c.reasons)


def test_a_minor_pr_with_no_outside_commits_and_an_owner_waiver_keep_their_own_reasons():
    assert cls([f("src/app.css")], authors=[OWNER], labels=["minor"]).reasons[0].startswith("no commits outside")
    assert cls([f("src/app.css")], labels=["minor", "review-waived"], waived_by=OWNER).cls == "waived"


def test_an_empty_minor_label_switches_the_skip_off():
    arch = {**RULES_JSON["architecture_review"], "minor_label": ""}
    off = Rules.from_json({**RULES_JSON, "architecture_review": arch})
    assert cls([f("src/app.css")], labels=["minor", ""], rules=off).cls == "normal"


def test_a_decision_on_a_minor_pr_is_green_without_reading_reviews():
    c = cls([f("src/app.css")], labels=["minor"])
    d = decide(c, REVIEWER, HEAD, [], lambda _: False)
    assert d.green and not d.request
    assert "labelled 'minor'" in d.status


def test_unlinked_commit_authors_count_as_outside_the_exempt_list():
    c = cls([f("src/app.ts")], authors=[OWNER, ""])
    assert c.cls == "normal"
    assert any("(unlinked author)" in r for r in c.reasons)


def test_docs_only_needs_no_review():
    assert cls([f("docs/guide.md", 900), f("README.md")]).cls == "not-required"


def test_a_doc_on_a_critical_path_is_critical():
    c = cls([f("docs/a.md"), f("tools/CLAUDE.md")])
    assert (c.cls, c.effort) == ("critical", "max")
    assert c.reasons[0] == "critical paths: tools/CLAUDE.md"


def test_a_file_renamed_away_from_a_critical_path_is_critical():
    assert cls([f("src/other/rate.ts", previous_filename="src/calc/rate.ts")]).cls == "critical"


def test_a_cut_short_file_list_is_critical():
    c = cls([f("src/app.ts")], changed=3001)
    assert c.cls == "critical"
    assert "only 1 of 3001" in c.reasons[0]


def test_large_counts_lines_outside_docs_and_tests_only():
    assert cls([f("src/app.ts", 400), f("tests/a.test.ts", 5000), f("docs/x.md", 5000)]).cls == "normal"
    c = cls([f("src/app.ts", 401)])
    assert (c.cls, c.effort) == ("large", "xhigh")


def test_tests_only_is_reviewed_until_decision_d8_turns_the_skip_on():
    files = [f("tests/unit/a.test.ts"), f("src/b.test.ts"), f("docs/x.md")]
    assert cls(files).cls == "normal"
    skip = Rules.from_json(
        {**RULES_JSON, "architecture_review": {**RULES_JSON["architecture_review"], "skip_test_only": True}}
    )
    assert cls(files, rules=skip).cls == "not-required"
    assert cls([*files, f("package.json")], rules=skip).cls == "critical"
    assert cls([*files, f("src/app.ts")], rules=skip).cls == "normal"


def test_waiver_counts_only_when_the_owner_added_it():
    files = [f(".github/workflows/ci.yml")]
    assert cls(files, labels=["review-waived"], waived_by=OWNER).cls == "waived"
    c = cls(files, labels=["review-waived"], waived_by=AGENT)
    assert c.cls == "critical"
    assert any("ignored: added by 'agent-app[bot]'" in r for r in c.reasons)
    assert cls(files, labels=["review-waived"]).reasons[-1].startswith("'review-waived' ignored: added by 'unknown'")


def test_a_rename_from_code_to_docs_is_not_docs_only():
    # Yousef's probe: src/app/x.tsx -> docs/x.md came out not-required.
    assert cls([f("docs/x.md", previous_filename="src/app/x.tsx")]).cls == "normal"
    assert cls([f("docs/x.md", previous_filename="docs/old/x.md")]).cls == "not-required"
    skip = Rules.from_json(
        {**RULES_JSON, "architecture_review": {**RULES_JSON["architecture_review"], "skip_test_only": True}}
    )
    assert cls([f("tests/a.test.ts", previous_filename="src/a.ts")], rules=skip).cls == "normal"


def owner_commit(message):
    return {"author": {"login": OWNER}, "commit": {"author": {"email": "o@x"}, "message": message}}


@pytest.mark.parametrize(
    "message",
    [
        "DFP-1: fix\n\nAgent: Malek\nRequested-by: Owner",
        "fix\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
        "fix\n\nco-authored-by:   claude <x>",
    ],
)
def test_an_agent_commit_under_the_owners_account_still_needs_review(message):
    # Yousef's probe: an agent commit authored as the owner, touching protected code, came out not-required.
    author = commit_author(owner_commit(message))
    assert author == f"{OWNER} (agent commit)"
    c = cls([f("src/calc/rate.ts")], authors=[author])
    assert (c.cls, c.effort) == ("critical", "max")


def test_commit_author_without_an_agent_mark():
    assert commit_author(owner_commit("fix\n\nThe agent: none here\nCo-Authored-By: Jane <j@x>")) == OWNER
    assert commit_author({"author": None, "commit": {"author": {"email": "m@x"}, "message": "x"}}) == "m@x"
    assert commit_author({"commit": {"message": "Agent: Malek"}}) == "unknown (agent commit)"
    assert commit_author({}) == ""


# --- review state ---------------------------------------------------------------------------------

CRITICAL = cls([f(".github/x.yml")])


def r(state, commit=HEAD, login=REVIEWER):
    return Review(login=login, state=state, commit_id=commit)


def never(_sha):
    raise AssertionError("the carry rule must not be consulted")


def test_not_required_is_green_without_reading_reviews():
    d = decide(cls([f("README.md")]), REVIEWER, HEAD, [], never)
    assert (d.state, d.request) == ("green", False)
    assert d.status.startswith("no review needed (not-required: docs only")


def test_no_review_yet_waits_and_requests_the_reviewer():
    d = decide(CRITICAL, REVIEWER, HEAD, [], never)
    assert (d.state, d.request, d.green) == ("waiting", True, False)
    assert d.status == f"waiting for {REVIEWER} (tier max)"


def test_approval_on_the_head_commit_is_green():
    d = decide(CRITICAL, REVIEWER, HEAD, [r("APPROVED")], never)
    assert (d.state, d.reviewed_sha, d.request) == ("green", HEAD, False)


def test_approval_of_an_older_commit_is_carried_when_the_change_set_is_identical():
    d = decide(CRITICAL, REVIEWER, HEAD, [r("APPROVED", OLD)], lambda sha: sha == OLD)
    assert (d.state, d.reviewed_sha) == ("green", OLD)
    assert "carried from ooooooo: same changes" in d.status


def test_approval_of_an_older_commit_with_new_changes_asks_for_a_delta_review():
    d = decide(CRITICAL, REVIEWER, HEAD, [r("APPROVED", OLD)], lambda _: False)
    assert (d.state, d.reviewed_sha, d.request) == ("waiting", OLD, True)
    assert "review the delta ooooooo..hhhhhhh (tier max)" in d.status


def test_changes_requested_on_the_head_is_the_authors_turn():
    d = decide(CRITICAL, REVIEWER, HEAD, [r("APPROVED", OLD), r("CHANGES_REQUESTED")], never)
    assert (d.state, d.request) == ("changes", False)


def test_new_commits_after_changes_requested_wait_for_the_reviewer_again():
    d = decide(CRITICAL, REVIEWER, HEAD, [r("CHANGES_REQUESTED", OLD)], never)
    assert (d.state, d.request, d.reviewed_sha) == ("waiting", True, OLD)


def test_a_dismissed_review_waits_for_a_new_one():
    d = decide(CRITICAL, REVIEWER, HEAD, [r("APPROVED"), r("DISMISSED")], never)
    assert (d.state, d.request) == ("waiting", True)


def test_comments_and_other_peoples_reviews_change_nothing():
    reviews = [r("APPROVED"), r("COMMENTED"), r("CHANGES_REQUESTED", login="someone"), r("PENDING")]
    assert latest_review(reviews, REVIEWER.upper()).state == "APPROVED"
    assert decide(CRITICAL, REVIEWER, HEAD, reviews, never).green


def test_review_from_github_reads_login_state_and_commit():
    assert Review.from_github({"user": {"login": "x"}, "state": "APPROVED", "commit_id": HEAD}) == Review(
        "x", "APPROVED", HEAD
    )
    assert Review.from_github({}) == Review("", "", "")


def test_change_set_is_unknown_when_missing_or_cut_short():
    assert change_set(None) is None
    assert change_set([{"filename": str(i)} for i in range(300)]) is None
    one = [{"filename": "a", "status": "modified", "sha": "1"}]
    assert change_set(one) == change_set(list(one))
    assert change_set(one) != change_set([{"filename": "a", "status": "modified", "sha": "2"}])


# --- command line ---------------------------------------------------------------------------------


class FakeGitHub(GitHub):
    """Answers GET paths from a dict (an exception value raises it) and records POSTs."""

    def __init__(self, responses, post_error=""):
        super().__init__(run=None)
        self.responses = responses
        self.calls = []
        self.posts = []
        self.post_error = post_error

    def _answer(self, path):
        self.calls.append(path)
        for prefix, value in self.responses.items():
            if path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise FetchError(f"GET {path}: 404")

    def get(self, path, raw=False):
        return self._answer(path)

    def items(self, path):
        return self._answer(path)

    def post(self, path, fields):
        self.posts.append((path, fields))
        return self.post_error


REPO = "org/repo"
PR = {
    "number": 7,
    "draft": False,
    "head": {"sha": HEAD},
    "base": {"ref": "main"},
    "user": {"login": AGENT},
    "labels": [],
    "requested_reviewers": [],
    "changed_files": 1,
}


def responses(pr=None, files=None, reviews=(), commits=None, **extra):
    base = f"repos/{REPO}"
    out = {
        f"{base}/contents/": RULES_JSON,
        f"{base}/pulls/7/commits": commits if commits is not None else [{"author": {"login": AGENT}}],
        f"{base}/pulls/7/files": files if files is not None else [{"filename": "src/calc/a.ts", "changes": 3}],
        f"{base}/pulls/7/reviews": [{"user": {"login": REVIEWER}, "state": s, "commit_id": c} for s, c in reviews],
        f"{base}/pulls/7": pr if pr is not None else PR,
    }
    out.update(extra)
    return out


def run(gh, *extra, mode="on", tmp_path=None):
    argv = ["--repo", REPO, "--pr", "7", "--head-sha", HEAD, "--rules-ref", BASE, "--mode", mode, *extra]
    if tmp_path is not None:
        argv += ["--summary", str(tmp_path / "summary.md")]
    return main(argv, gh=gh)


def test_off_reads_nothing_and_passes(capsys):
    gh = FakeGitHub({})
    assert run(gh, mode="OFF") == 0
    assert gh.calls == []
    assert "off" in capsys.readouterr().out


def test_an_unknown_mode_fails_visibly(capsys):
    assert run(FakeGitHub({}), mode="onn") == 1
    assert "ARCH_REVIEW_MODE is 'onn'" in capsys.readouterr().out


def test_on_mode_waits_requests_the_reviewer_and_fails(capsys, tmp_path):
    gh = FakeGitHub(responses())
    assert run(gh, tmp_path=tmp_path) == 1
    assert gh.posts == [(f"repos/{REPO}/pulls/7/requested_reviewers", [f"reviewers[]={REVIEWER}"])]
    assert gh.calls[0] == f"repos/{REPO}/contents/.github/review-rules.json?ref={BASE}"
    out = capsys.readouterr().out
    assert f"arch-review mode=on class=critical effort=max state=waiting head={HEAD} reviewed=-" in out
    assert f"::error title=Architecture review::waiting for {REVIEWER} (tier max) (requested {REVIEWER})" in out
    summary = (tmp_path / "summary.md").read_text()
    assert "| Class | critical (effort max) |" in summary
    assert f"| Request | requested {REVIEWER} |" in summary


@pytest.mark.parametrize(
    ("change", "note"),
    [
        ({"requested_reviewers": [{"login": REVIEWER}]}, "is already requested"),
        ({"draft": True}, "draft: "),
        ({"user": {"login": REVIEWER}}, "opened the PR and cannot review it"),
    ],
)
def test_the_reviewer_is_not_requested_twice_on_drafts_or_on_their_own_pr(change, note, capsys):
    gh = FakeGitHub(responses(pr={**PR, **change}))
    assert run(gh) == 1
    assert gh.posts == []
    assert note in capsys.readouterr().out


def test_a_refused_request_is_reported_and_the_check_stays_red(capsys):
    gh = FakeGitHub(responses(), post_error="HTTP 422: not a collaborator")
    assert run(gh) == 1
    assert "could not request arch-bot: HTTP 422" in capsys.readouterr().out


def test_no_request_mode_only_waits(capsys):
    gh = FakeGitHub(responses())
    assert run(gh, "--no-request") == 1
    assert gh.posts == []
    assert "(waits for a review)" in capsys.readouterr().out


def test_report_mode_is_always_green_and_says_what_on_would_do(capsys):
    gh = FakeGitHub(responses())
    assert run(gh, mode="") == 0
    assert gh.posts == []
    out = capsys.readouterr().out
    assert "mode=report" in out
    assert "with ARCH_REVIEW_MODE=on this would be red: waiting for arch-bot (tier max) (would request arch-bot)" in out


def test_approval_on_the_head_passes(capsys):
    gh = FakeGitHub(responses(reviews=[("APPROVED", HEAD)]))
    assert run(gh) == 0
    assert gh.posts == []
    assert "approved by arch-bot on the head commit" in capsys.readouterr().out


def compare(files):
    return {"files": files}


def test_carry_rule_through_the_compare_api():
    same = [{"filename": "src/calc/a.ts", "status": "modified", "sha": "blob1"}]
    gh = FakeGitHub(
        responses(
            reviews=[("APPROVED", OLD)],
            **{
                f"repos/{REPO}/compare/main...{OLD}": compare(same),
                f"repos/{REPO}/compare/main...{HEAD}": compare(same),
            },
        )
    )
    assert run(gh) == 0

    changed = [{"filename": "src/calc/a.ts", "status": "modified", "sha": "blob2"}]
    gh = FakeGitHub(
        responses(
            reviews=[("APPROVED", OLD)],
            **{
                f"repos/{REPO}/compare/main...{OLD}": compare(same),
                f"repos/{REPO}/compare/main...{HEAD}": compare(changed),
            },
        )
    )
    assert run(gh) == 1
    assert gh.posts  # re-requested for the delta


def test_an_unreachable_approved_commit_means_re_review():
    gh = FakeGitHub(responses(reviews=[("APPROVED", OLD)]))  # compare answers 404
    assert run(gh) == 1


def test_waiver_by_the_owner_passes_without_reading_reviews():
    pr = {**PR, "labels": [{"name": "review-waived"}]}
    events = [{"event": "labeled", "label": {"name": "review-waived"}, "actor": {"login": OWNER}}]
    gh = FakeGitHub(responses(pr=pr, **{f"repos/{REPO}/issues/7/events": events}))
    assert run(gh) == 0
    assert not any("reviews" in c for c in gh.calls)


def test_a_moved_head_fails_closed(capsys):
    gh = FakeGitHub(responses(pr={**PR, "head": {"sha": "n" * 40}}))
    assert run(gh) == 1
    assert "the PR head moved to nnnnnnn" in capsys.readouterr().out


def test_unreadable_rules_fail_closed_in_on_and_pass_in_report(capsys, tmp_path):
    gh = FakeGitHub({f"repos/{REPO}/contents/": FetchError("GET rules: 404")})
    assert run(gh, tmp_path=tmp_path) == 1
    assert "state=error" in capsys.readouterr().out
    assert "| Error | GET rules: 404 |" in (tmp_path / "summary.md").read_text()
    assert run(FakeGitHub({f"repos/{REPO}/contents/": {"owner": ""}}), mode="report") == 0


def test_summary_cells_cannot_break_the_table_or_add_lines(tmp_path):
    files = [{"filename": ".github/a|b\n::warning::c.yml", "changes": 1}]
    gh = FakeGitHub(responses(files=files))
    assert run(gh, tmp_path=tmp_path) == 1
    summary = (tmp_path / "summary.md").read_text()
    assert "critical paths: .github/a\\|b ::warning::c.yml" in summary
    assert all(line.startswith(("|", "#")) or not line for line in summary.splitlines())


def test_an_unlinked_commit_author_is_named_by_email(tmp_path):
    commits = [{"author": None, "commit": {"author": {"email": "malek@example.com"}}}]
    gh = FakeGitHub(responses(commits=commits, files=[{"filename": "src/app.ts", "changes": 1}]))
    assert run(gh, mode="report", tmp_path=tmp_path) == 0
    assert "| Why | 1 changed lines; commits by malek@example.com |" in (tmp_path / "summary.md").read_text()


def test_commits_the_api_did_not_list_count_as_outside_the_exempt_list(tmp_path):
    gh = FakeGitHub(responses(pr={**PR, "commits": 300}, commits=[{"author": {"login": OWNER}}] * 250))
    assert run(gh, tmp_path=tmp_path) == 1
    assert "commits not listed by the API" not in (tmp_path / "summary.md").read_text()  # critical wins
    gh = FakeGitHub(
        responses(
            pr={**PR, "commits": 300},
            commits=[{"author": {"login": OWNER}}] * 250,
            files=[{"filename": "src/app.ts", "changes": 1}],
        )
    )
    assert run(gh, tmp_path=tmp_path) == 1
    assert "commits by (50 commits not listed by the API)" in (tmp_path / "summary.md").read_text()


def test_an_unexpected_error_is_red_in_on_and_green_in_report(capsys, tmp_path):
    # Yousef's probe: an odd API answer (here a commit that isn't an object) gave a traceback and a red report.
    odd = responses(commits=["not an object"])
    assert run(FakeGitHub(odd), mode="report", tmp_path=tmp_path) == 0
    out = capsys.readouterr().out
    assert "state=error" in out
    assert "this would be red: unexpected AttributeError" in out
    assert "| Error | unexpected AttributeError:" in (tmp_path / "summary.md").read_text()
    assert run(FakeGitHub(odd), mode="on") == 1
    assert "::error title=Architecture review::unexpected AttributeError" in capsys.readouterr().out


def test_an_agent_commit_under_the_owners_account_reaches_the_summary(tmp_path):
    commits = [owner_commit("fix\n\nAgent: Malek")]
    gh = FakeGitHub(responses(commits=commits, files=[{"filename": "src/app.ts", "changes": 1}]))
    assert run(gh, mode="report", tmp_path=tmp_path) == 0
    assert f"commits by {OWNER} (agent commit)" in (tmp_path / "summary.md").read_text()


def test_not_required_pr_passes_in_on_mode(capsys):
    gh = FakeGitHub(responses(commits=[{"author": {"login": OWNER}}]))
    assert run(gh) == 0
    assert "class=not-required effort=- state=green" in capsys.readouterr().out


def test_minor_pr_is_green_in_on_mode_and_requests_nobody(capsys):
    pr = {**PR, "labels": [{"name": "minor"}]}
    gh = FakeGitHub(responses(pr=pr, files=[{"filename": "src/app.css", "changes": 3}]))
    assert run(gh) == 0
    assert "class=not-required effort=- state=green" in capsys.readouterr().out
    assert gh.posts == []


def test_minor_label_on_a_critical_path_pr_stays_red_in_on_mode(capsys):
    pr = {**PR, "labels": [{"name": "minor"}]}
    gh = FakeGitHub(responses(pr=pr))  # the default file is src/calc/a.ts, a critical path
    assert run(gh) == 1
    out = capsys.readouterr().out
    assert "class=critical effort=max state=waiting" in out
    assert gh.posts and "requested_reviewers" in gh.posts[0][0]


# --- the gh wrapper -------------------------------------------------------------------------------


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


def test_github_get_asks_for_raw_content_and_parses_json():
    seen = []
    gh = GitHub(run=lambda args: seen.append(args) or done(out=json.dumps({"a": 1})))
    assert gh.get("repos/x/contents/f", raw=True) == {"a": 1}
    assert seen == [["-H", "Accept: application/vnd.github.raw+json", "repos/x/contents/f"]]


def test_github_errors_and_bad_json_raise_fetch_error():
    with pytest.raises(FetchError, match="HTTP 404"):
        GitHub(run=lambda args: done(1, err="HTTP 404")).get("p")
    with pytest.raises(FetchError, match="not JSON"):
        GitHub(run=lambda args: done(out="<html>")).get("p")
    with pytest.raises(FetchError):
        GitHub(run=lambda args: done(1, out="boom")).items("p")


def test_github_items_reads_one_object_per_line_across_pages():
    seen = []
    gh = GitHub(run=lambda args: seen.append(args) or done(out='{"a": 1}\n\n{"a": 2}\n'))
    assert gh.items("repos/x/pulls/1/files") == [{"a": 1}, {"a": 2}]
    assert seen[0][:3] == ["--paginate", "--jq", ".[] | tojson"]


def test_github_post_returns_the_error_text():
    seen = []
    assert GitHub(run=lambda args: seen.append(args) or done()).post("p", ["reviewers[]=x"]) == ""
    assert seen == [["-X", "POST", "p", "-f", "reviewers[]=x"]]
    assert GitHub(run=lambda args: done(1, err="HTTP 422")).post("p", []) == "HTTP 422"
