# Merge train

ADR-020 decision 3 and Enforcement E1 (BASITA-204; the process side is BASITA-200). A wave of N pull requests costs about N merge-ups instead of up to N(N-1)/2, because after a merge **only the next open pull request in the published order is brought up to date**. The train never merges: only Ahmed merges (AGENTS.md), and auto-merging would need his separate, written delegation.

GitHub's merge queue is not available to us (private repositories need Enterprise Cloud), so the train is a written order plus this automation.

## The pieces

| Piece | Where | Who writes it |
|---|---|---|
| The order, `{"wave": "…", "order": [226, 227, 230]}` | `.github/merge-train.json` on the app repository's default branch | Sherif publishes it per wave; Ahmed or Talia changes it, in a pull request |
| The decisions and the GitHub calls | `src/merge_train/` here (batch mode in `batch.py`), tested in `tests/test_merge_train*.py` | Malek |
| The reusable workflow | `.github/workflows/reusable-merge-train.yml` here, from `templates/merge-train/` | Ahmed installs it |
| The caller | `.github/workflows/merge-train.yml` in the app repository, from `templates/merge-train/` | Ahmed installs it |
| The `basita-train` GitHub App | created from `config/github-identities.json` (`train`) | Ahmed |

The workflow files are templates only because no agent App can push `.github/workflows/` (decision D4, [agent identity](06-agent-identity.md)).

## Batch mode (the default, `batch-size: 3`)

Yousef's amendment of 17/11/2026 (BASITA-233 / BASITA-204, D1–D9) is the source for this section. Ahmed's reason: a full E2E per pull request (≈ 20 min on the Mac) costs too much. So the train runs **one full E2E per batch of k pull requests** and **never updates a pull-request branch**. The per-pull-request flow below stays as the fallback (`batch-size: 0`). The order file, the App, the pinning, the comment markers, the triggers and «جاهز للدمج» with its «الإعلان يشمل …» line are unchanged. The train still never merges: the merge follows the delegation in force (`agent-gh talia pr merge`) or Ahmed. The nightly full E2E on `main` stays as the backstop.

### Who joins a batch (D1)
A pull request is a **candidate** when it is open and not a draft, its base is the default branch (not stacked), it carries `run-e2e`, and `ci / Build and test` and `Owner review` are green on its head. The engineer sets `run-e2e` at the QA hand-off; a pull request without it never needs E2E and goes through the plain ready check, as in the fallback. **A batch is the first k candidates in the published order** (default 3, max 4). A non-candidate earlier in the order doesn't block later ones; the report names it and says why. Owner review must already be green, because a batch is tested as a whole and merged in order, so a member waiting for review would block everyone after it. Since no head changes, **Ahmed labels `reviewed-by-ahmed` once, whenever he has reviewed, and the label stays.**

### Building the batch: the API only (D2)
One fixed branch, `train/run` (a constant in the code, never an input), is force-updated for each batch:
1. Point `train/run` at the default branch's SHA, the base (`PATCH /git/refs/heads/train/run`, or `POST /git/refs` the first time).
2. For each member in order: `POST /merges` with `base: train/run`, `head: <pinned head SHA>` and `commit_message: "train <wave>#<batch>: #<N>@<sha>"`. GitHub builds the merge server-side, so the train runs no pull-request code. **409 (conflict):** that member leaves the batch and gets the `train-conflict` comment and label (the existing `conflicts.py` route, keyed on head + base). The rest continue.
3. The tip's commit messages are the **manifest** (base, then `#N@sha` in order). The state lives in the branch itself, and a later run re-reads it. There is no other store.

**The manifest is trusted only when GitHub built it** (Yousef's review, M1). There is no branch protection, so anyone with write access could push a commit whose message looks like a manifest line. A manifest commit counts only when its parents are exactly [the previous manifest commit, or the base for the first one; the pinned head its message names], and GitHub verified its signature (`verification.verified`; `POST /merges` commits are signed by web-flow). If any commit fails, the whole manifest is untrusted: the batch is dropped (its ready comments withdrawn), rebuilt from the current state, and the report carries an ATTENTION line.

A pull request whose head conflicted on this `main` is not a candidate again until its head or `main` moves (the conflict comment is the record), so a run never rebuilds the same conflict.

Every app-repository workflow with a `push` trigger is limited to `main`, so pushing `train/run` starts nothing but the dispatch below. A test must pin this (D9); the playbook's templates test reads only `templates/merge-train/`, not the app repository's workflows, so that test is still open (Basel).

### Excluded from batching (D3)
A pull request that changes `.github/workflows/**` runs as a **batch of one**, so its workflow change never becomes part of another pull request's gate. First in line, it is the batch; further down, the batch ends before it and it goes alone next time. A pull request that is a draft, or whose CI is red, is not a candidate.

### Running E2E (D4)
The train dispatches `e2e.yml` with `ref: train/run` (`POST /actions/workflows/e2e.yml/dispatches`), right after it builds the batch, and again only when no run of the tip is visible after `settle-seconds`. The run's `head_sha` is the train tip, so `E2E (Playwright)` lands on that SHA and the train reads it with `check_runs()`. The batch runs the `e2e.yml` in the batch tree. That is the same trust as a label run, which runs the pull request's own `e2e.yml`, and D3 keeps a workflow-changing pull request alone. `e2e.yml`'s `concurrency` keys on `refs/heads/train/run`, so a new batch cancels a stale run.

### Green batch (D5)
For each member, in order, the train posts «جاهز للدمج (batch <wave>#<b>, <i>/<k>): بعد #<previous>», keyed on head + batch, only if: the head is still the pinned SHA; `main` equals the batch base or has moved **only** by the earlier members, in order (`GET /commits/{sha}/pulls` names the member; squash and merge commits both count); and CI and Owner review are still green on that head. **Members are merged strictly in order**, and the comment names the one that must merge first. A CI or Owner review that is running again on a member (a re-run on a label event) holds the signal until it is green; it does not drop the batch. Failed or gone does. A gate (CI, Owner review or the batch's E2E) that stays pending for more than 2 hours gets an ATTENTION line in the report; the batch is still held, nothing else changes.

### Invalidation (D6)
A batch is dropped, and the next run rebuilds it from the current state, when: a member's head moves; a member is closed, turns draft, or loses Owner review or CI green; `main` moves by a commit that is not the next member; or the published order changes. Its `ready` comments get «سُحب: batch اتلغى (<reason>)» under the same marker family, so nobody merges on a stale signal.

### Red batch (D7)
1. **One re-run of the same tip** (`POST /actions/runs/{id}/rerun-failed-jobs`). If it's green, the batch goes on as **flaky**: the train comments the failing test names, and a Salma flake ticket is due (through the Mac-side check, like `train-conflict`).
2. **Red again → prefix bisect**, because order matters. Test `[1]`: if it's red, #1 is the culprit. Otherwise test `[1,2]`, and so on. That is at most k−1 extra runs (2 for k = 3). A prefix is a `train/run` built from the same base with fewer members; each of its merge commits adds the line `Train-Bisect: <full tip> #N@sha …` (the full red batch), so the bisect state is in the branch too. Prefix runs are not re-run.
3. The culprit gets `train-red` and one comment with the failing tests and the run link, and it goes back to Malek. The green prefix gets «جاهز للدمج» (D5). Members after the culprit go back to the queue.

### After the merges: the tree check (D8)
When the last member of a green batch has merged, the train compares the tree of `main` right after that merge with the tree that was tested. Equal: done. Different (a squash or merge edge case, or a hand change): it says so in a comment on the last member and in the run's report. **It dispatches nothing on `main`** (Yousef, record 5d9d0b13, which overrides the amendment's "dispatch on main"): the nightly E2E on `main` is the backstop, and Ahmed or an engineer runs E2E on `main` by hand.

### Pull requests without `run-e2e` in batch mode
They get the plain ready check: «جاهز للدمج» when CI and Owner review are green on the head and it is up to date with `main`. The train never updates their branch either; one that is behind is reported. While a batch is live, the comment says «جاهز للدمج: #N بعد الـ batch الحالي (<wave>#<b>: #a ثم #b)» and that merging it first drops the batch.

### When pull-request E2E switches to `@smoke`
Only on the day the batch train is installed and has passed one real batch. Until then, the full `run-e2e` on the pull request stays the gate.

## Per-pull-request mode (`batch-size: 0`, the fallback)

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

**In batch mode this section doesn't apply:** no head is changed, so the label is given once and stays (D1). In the fallback mode: **Ahmed adds `reviewed-by-ahmed` to a train pull request only after it is up to date, never in advance.** The train's `update-branch` is a new commit on the pull request (a `synchronize` event), and Owner review withdraws the label on every new commit (`owner-review.yml`, "New commits withdraw an earlier sign-off"). A critical pull request labelled before it became the head of the train loses the label when the train updates it, and Ahmed would review again. The flow is consistent because «جاهز للدمج» already needs Owner review green on the *current* head: label after the update, then the comment appears (the `labeled` trigger below re-runs the train).

## Triggers and cost

`push` to the default branch and `workflow_dispatch` (the ADR), plus `workflow_run` when `CI`, `E2E` or `Owner review` complete and `pull_request_target: labeled` for `reviewed-by-ahmed`, so «جاهز للدمج» appears when the head turns green. A run that would only be skipped gets its own concurrency group, so it can never replace a waiting real run (the e2e.yml lesson). About 1 slim-minute per event ($0.002); the ADR estimates $0.5-1 a month.

## Permissions

Reads use the workflow's `GITHUB_TOKEN` (`contents`, `pull-requests`, `checks`, `statuses` read). Writes use the `basita-train` App's token, minted by `actions/create-github-app-token` with `permission-contents: write`, `permission-pull-requests: write` and, for batch mode, `permission-actions: write` (the dispatch and the re-run; D9), for this repository only. Batch mode also reads E2E runs, so the workflow's `GITHUB_TOKEN` needs `actions: read`. The write methods, pinned by a test: in batch mode, update-ref on `train/run` **only**, `POST /merges` with base `train/run` **only**, dispatch of `e2e.yml` **only**, rerun-failed-jobs, add or remove a label, post a comment; in the fallback, update-branch, labels and comments. There is no merge call into the default branch, no ref write outside `train/run`, and no cancel call. The ref name and the workflow name are constants in the code, never inputs. Because `actions: write` could dispatch, cancel or re-run any workflow, the client fences it itself, before any HTTP call (Yousef, record 5d9d0b13): dispatch only `e2e.yml` and only on `train/run`; re-run failed jobs only on a run it has read first and found to be an `e2e.yml` run on `train/run`. `tests/test_merge_train_fence.py` pins these refusals. The App's pushes are merge commits on pull-request branches; the Main guard (red) and the Mac's hourly check (attention line) already flag any update to `main` by an actor that is not Ahmed, which includes this App.

## Install (Ahmed)

1. `scripts/gh-app-new train` (dry run first with `--dry-run`): creates `basita-train` in the `basitasolutions` organisation and stores its key in the Keychain; install it on **the app repository only**; add the ids to `config/github-identities.json`.
2. In the app repository, add two Actions secrets: `TRAIN_APP_ID`, and `TRAIN_APP_PRIVATE_KEY` from the Keychain: `security find-generic-password -a basita -s basita-gh-train-app -w | base64 -d | gh secret set TRAIN_APP_PRIVATE_KEY -R basitasolutions/elbayoumy-farm-nutrition-platform`.
3. Copy `templates/merge-train/reusable-merge-train.yml` to `.github/workflows/reusable-merge-train.yml` here; push to `main`. Note that commit's full SHA: it has both the workflow and `src/merge_train/`.
4. In the app repository, copy `templates/merge-train/merge-train.yml` to `.github/workflows/merge-train.yml` and `templates/merge-train/merge-train.json` to `.github/merge-train.json` (a PR; the workflow file needs Ahmed's `reviewed-by-ahmed`). **Replace both `PLAYBOOK_COMMIT_SHA` in the caller with that SHA** (the `@` of the workflow reference and `playbook-ref`).
5. Try it: Actions, "Merge train", Run workflow, with an empty `order` (it does nothing), then with a real wave.

## Pinning

The caller passes the App's private key (write access to the app repository) to the reusable workflow, and that workflow checks out and runs `merge_train` from the playbook. If both followed `main`, any push to the playbook's `main` would run with that key. So the caller pins **both** to one reviewed commit: the reference `reusable-merge-train.yml@<sha>` and `playbook-ref: <sha>` (no default, never a branch). The workflow's first step stops the run unless `playbook-ref` is a full 40-character SHA, before the key is minted, and the template ships with the placeholder `PLAYBOOK_COMMIT_SHA`, so an install that forgot to pin fails closed. To change the train's behaviour: merge the playbook change, then bump the SHA in a pull request on the app repository (a workflow file, so it needs Ahmed's label). A test pins this (`tests/test_merge_train_templates.py`).

## Not in this change

- D8 follow-up: when the trees differ, point `train/run` at `main`'s commit and dispatch E2E there (the fence allows `train/run` only), instead of the manual run on `main`.
- Telling Malek and Talia's queue about `train-conflict` and «جاهز للدمج» is a Mac-side check in basita-ops (it reads the labels and attention lines), because the workflow has no Jira access and should not.
- `registers-guard`, the Jira "Register IDs" check and the hub-change label (E2, E3, E4) are separate tickets.
