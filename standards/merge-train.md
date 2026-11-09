# Merge train

ADR-020 decision 3 and Enforcement E1 (BASITA-204; the process side is BASITA-200). A wave of N pull requests costs about N merge-ups instead of up to N(N-1)/2, because after a merge **only the next open pull request in the published order is brought up to date**. The train never merges: only Ahmed merges (AGENTS.md), and auto-merging would need his separate, written delegation.

GitHub's merge queue is not available to us (private repositories need Enterprise Cloud), so the train is a written order plus this automation.

## The pieces

| Piece | Where | Who writes it |
|---|---|---|
| The order, `{"wave": "…", "order": [226, 227, 230]}` | `.github/merge-train.json` on the app repository's default branch | Sherif publishes it per wave; Ahmed or Talia changes it, in a pull request |
| The decisions and the GitHub calls | `src/merge_train/` here, tested in `tests/test_merge_train.py` | Malek |
| The reusable workflow | `.github/workflows/reusable-merge-train.yml` here, from `templates/merge-train/` | Ahmed installs it |
| The caller | `.github/workflows/merge-train.yml` in the app repository, from `templates/merge-train/` | Ahmed installs it |
| The `basita-train` GitHub App | created from `config/github-identities.json` (`train`) | Ahmed |

The workflow files are templates only because no agent App can push `.github/workflows/` (decision D4, [agent identity](06-agent-identity.md)).

## What one run does

It takes the first open pull request in `order` (the head of the train; merged and closed ones are skipped) and:

1. **Draft, or based on another branch** (stacked on another pull request): reports and touches nothing. A stacked pull request waits until its base merges.
2. **Behind the default branch** (`compare` says `behind_by > 0`; `mergeable_state: behind` needs branch protection, which Free lacks): `PUT /pulls/{n}/update-branch` with `expected_head_sha`.
   - **Conflict:** one comment per head and `main` commit with the conflicting files (`git merge-tree --write-tree`, which reads objects and runs nothing from the pull request) and the label `train-conflict`. A GitHub App cannot be an assignee, so the label and the comment name Malek; a Mac-side check routes it (see "Not in this change").
   - **Updated:** it waits for the new head, then asks for E2E **once** (below).
3. **E2E:** when the pull request carries `run-e2e` and no E2E has run on this head, it removes the label and adds it again (adding a label that is already there fires nothing), and says so in one comment keyed on the head. A pull request without `run-e2e` never asked for E2E (docs-only, CI-only), so none is requested.
4. **Ready:** when `ci / Build and test` and `Owner review` are green on the current head, and `E2E (Playwright)` is green when the pull request carries `run-e2e`, it posts one comment per head: «جاهز للدمج: #N (CI ✓، E2E ✓، Owner review ✓، reviewed-by-ahmed ✓)». Owner review is green only when Ahmed's label is there or the pull request does not need it, so it is the single check that encodes "reviewed". **What the signal covers, and what it does not:** only CI, E2E (when labelled) and Owner review decide it. Every other check run (Architecture review, `ai-guard`, `agent-attribution`, the dependency audit) and the commit status `Jira status` do not. The comment therefore always says «الإعلان يشمل CI وE2E وOwner review فقط» and names anything else that is red or still running, with `Jira status` read from the commit statuses (a DFP issue that is not yet Ready for Release shows up there). «جاهز للدمج» is never «everything is green»: look at the line under it before merging.
   - A pull request that changes only documentation never starts CI (path filter). Absent CI counts as fine when `Owner review` has reported and the head has had `settle-seconds` (120) to show its checks.

Every comment carries a hidden `<!-- merge-train:<kind>:<key> -->` marker, so a second run says nothing twice. Only comments written by the train's own App (`basita-train[bot]`) count as markers: a human's comment can neither suppress a report nor fake «جاهز للدمج». Runs are idempotent, which is why extra triggers are harmless.

## Owner sign-off and the train's updates (read this before labelling)

**Ahmed adds `reviewed-by-ahmed` to a train pull request only after it is up to date, never in advance.** The train's `update-branch` is a new commit on the pull request (a `synchronize` event), and Owner review withdraws the label on every new commit (`owner-review.yml`, "New commits withdraw an earlier sign-off"). A critical pull request labelled before it became the head of the train loses the label when the train updates it, and Ahmed would review again. The flow is consistent because «جاهز للدمج» already needs Owner review green on the *current* head: label after the update, then the comment appears (the `labeled` trigger below re-runs the train).

## Triggers and cost

`push` to the default branch and `workflow_dispatch` (the ADR), plus `workflow_run` when `CI`, `E2E` or `Owner review` complete and `pull_request_target: labeled` for `reviewed-by-ahmed`, so «جاهز للدمج» appears when the head turns green. A run that would only be skipped gets its own concurrency group, so it can never replace a waiting real run (the e2e.yml lesson). About 1 slim-minute per event ($0.002); the ADR estimates $0.5-1 a month.

## Permissions

Reads use the workflow's `GITHUB_TOKEN` (`contents`, `pull-requests`, `checks`, `statuses` read). Writes use the `basita-train` App's token, minted by `actions/create-github-app-token` with `permission-contents: write` and `permission-pull-requests: write` for this repository only. The write methods are update-branch, add or remove a label, post a comment: there is no merge call, and a test pins the list. The App's pushes are merge commits on pull-request branches; the Main guard (red) and the Mac's hourly check (attention line) already flag any update to `main` by an actor that is not Ahmed, which includes this App.

## Install (Ahmed)

1. `scripts/gh-app-new train` (dry run first with `--dry-run`): creates `basita-train` in the `basitasolutions` organisation and stores its key in the Keychain; install it on **the app repository only**; add the ids to `config/github-identities.json`.
2. In the app repository, add two Actions secrets: `TRAIN_APP_ID`, and `TRAIN_APP_PRIVATE_KEY` from the Keychain: `security find-generic-password -a basita -s basita-gh-train-app -w | base64 -d | gh secret set TRAIN_APP_PRIVATE_KEY -R basitasolutions/elbayoumy-farm-nutrition-platform`.
3. Copy `templates/merge-train/reusable-merge-train.yml` to `.github/workflows/reusable-merge-train.yml` here; push to `main`. Note that commit's full SHA: it has both the workflow and `src/merge_train/`.
4. In the app repository, copy `templates/merge-train/merge-train.yml` to `.github/workflows/merge-train.yml` and `templates/merge-train/merge-train.json` to `.github/merge-train.json` (a PR; the workflow file needs Ahmed's `reviewed-by-ahmed`). **Replace both `PLAYBOOK_COMMIT_SHA` in the caller with that SHA** (the `@` of the workflow reference and `playbook-ref`).
5. Try it: Actions, "Merge train", Run workflow, with an empty `order` (it does nothing), then with a real wave.

## Pinning

The caller passes the App's private key (write access to the app repository) to the reusable workflow, and that workflow checks out and runs `merge_train` from the playbook. If both followed `main`, any push to the playbook's `main` would run with that key. So the caller pins **both** to one reviewed commit: the reference `reusable-merge-train.yml@<sha>` and `playbook-ref: <sha>` (no default, never a branch). The workflow's first step stops the run unless `playbook-ref` is a full 40-character SHA, before the key is minted, and the template ships with the placeholder `PLAYBOOK_COMMIT_SHA`, so an install that forgot to pin fails closed. To change the train's behaviour: merge the playbook change, then bump the SHA in a pull request on the app repository (a workflow file, so it needs Ahmed's label). A test pins this (`tests/test_merge_train_templates.py`).

## Not in this change

- Telling Malek and Talia's queue about `train-conflict` and «جاهز للدمج» is a Mac-side check in basita-ops (it reads the labels and attention lines), because the workflow has no Jira access and should not.
- `registers-guard`, the Jira "Register IDs" check and the hub-change label (E2, E3, E4) are separate tickets.
