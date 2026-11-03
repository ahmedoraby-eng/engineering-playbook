# Changelog

Notable changes to the playbook, newest first. Each entry links to the pull request that made it.

## 2026-11-03

### Added (BASITA-163, identities M7: App keys in cloud sessions; waits on M0)
- An App's key comes from `BASITA_GH_<AGENT>_KEY` (one base64 line of its own second PEM) when the machine has no Keychain, as in a cloud session. The Keychain always wins on the Mac. `basita-sync` and `basita-reader` (and any App that updates `main` or reads only) are refused even when the variable is set (plan 10.8); Yousef's user tokens stay Keychain-only. `gh-check` says where the key came from.
- REST mode (on when `CLAUDE_CODE_REMOTE=true`, or `AGENT_GH_REST=1|0`): `agent-gh` runs `pr create`, `pr view`, `pr comment` and `pr checks` through the REST API itself, after the policy approved the original command, because the proxy blocks GraphQL. Other `gh` commands that need GraphQL are refused with the way round (`agent-gh <agent> api …`).
- A `gh` write from a cloud session is refused until the proxy test says REST carries the App's token (`AGENT_GH_CLOUD_WRITES=1`); reads work.
- `agent-git <agent> push` in a cloud session is refused until the proxy test says the push carries the App's token; `AGENT_GIT_CLOUD_PUSH=git` turns it on, with the token as an `http.extraHeader` from the environment. The Git Data API push isn't built.
- `scripts/gh-cloud-key <agent> --file <.pem>` (setup S1, S8): checks the second key is this App's and not the Mac's, and puts one base64 line on the clipboard without printing it or storing it.
- `scripts/gh-proxy-test <agent> <owner/repo>` (M0, BASITA-156): REST, `gh api`, GraphQL and one push through the proxy with the App's own token, printing logins, statuses and yes/no only; `--whoami` for the `/web-setup` question.

### Added
- `atl <agent> jira-edit <KEY>` and `atl <agent> jira-link <KEY> <KEY> --type T` (BASITA-169), as the agent's own Jira user. `jira-edit` changes labels, assignee, description, priority, Fix Versions, summary, parent and due date in one request. It refuses the labels only Ahmed sets (`reviewed-by-ahmed`, `human-reviewed`, `needs-ahmed`, `jira-status-waived`, any case) in every form: add, remove, or a whole list. `jira-link` takes a link type's name or phrase, reads the link back and says what Jira recorded. They replace the Atlassian MCP's edits and links, which showed as Ahmed. `jira-create` also refuses the Ahmed-only labels in any case now. After Yousef's review: a whole label list keeps the Ahmed-only labels exactly, case included (`NEEDS-AHMED` no longer replaces `needs-ahmed`); both commands work in `BASITA`, `DFP` and `ELB` only; `jira-link` compares the read-back with the links there before the call.
- Every agent's own GitHub identity (BASITA-157, plan section 10). `config/github-identities.json` lists `basita-talia`, `-sherif`, `-adam`, `-nora`, `-yousef-docs`, `-sync` and `-reader`, each with its own `permissions` and a `manifest` for `gh-app-new`. They are pending until Ahmed creates them (S2): `agent-gh` and `agent-git push` refuse them with "not set up yet", and `gh-check` prints a plain `pending <agent>: not created yet` line.
- `agent-gh reader credential`: a read-only git credential helper for `https://github.com`. It mints a read-only token for the one repository in git's request and stores nothing. A push with it fails at GitHub, because the token can only read.
- GitHub identities for the agents that write code (BASITA-149). `config/github-identities.json` lists, without secrets, the Apps of Malek, Haya and Salma (App id, installations, bot id) and Yousef's user. `scripts/agent-gh <agent> <gh args>` runs gh with a token for that command only: an App's 1-hour installation token for the one repository, read-only for reads and `pull_requests:write` with `contents:read` for pull request writes, so it cannot merge or push. Yousef's token is chosen by the repository owner. It refuses merging, closing, deletes, settings, secrets, workflows, `gh auth`, App approvals and the labels only Ahmed sets.
- `scripts/agent-git <agent> push`: pushes with the App's `contents:write` token through a one-command credential helper that replaces the Mac's own login. Never to main or master, never forced, deleting, bulk or tags.
- `scripts/gh-check [agent]`: read-only. Login, App permissions, bot id, installations (an unlisted one is an attention line), the repos each reaches, and Yousef's token expiry (attention 14 days before). Never prints a token.
- `scripts/gh-store <agent>`: stores an App key or one of Yousef's tokens in the Keychain, only if it has the right shape.
- After Yousef's review: an App's token is revoked as soon as its command ends, even after a failure; the git credential helper answers only `https://github.com`; a remote needs exactly one push URL; submodules are never pushed; trace and `GIT_CONFIG_*` variables are cleared; Yousef gets reads, reviews and comments only; `gh-check` checks the webhook is off, pages installations, and fails if Yousef has push anywhere; `gh-store` passes the secret on stdin (BASITA-149).
- Architecture review (BASITA-150): a soft check that a PR carries the architect's review of its latest commit. `python -m arch_review` classifies the PR from `.github/review-rules.json` read from the base branch (not required, waived by the owner's `review-waived` label, critical = `max`, large = `xhigh`, normal = `high`), requests the reviewer itself, and stays red until the reviewer's latest review approves the head commit. An approval of an older commit carries when the change set is identical. The repo variable `ARCH_REVIEW_MODE` (`off`, `report`, `on`; unset means `report`) decides the effect. Reusable `reusable-architecture-review.yml` on `pull_request_target`, plus `reusable-architecture-review-rerun.yml`, which only re-runs it when the reviewer reviews. On this repo it waits for the review instead of requesting it (plan section 4.1a).
- Main guard (BASITA-150): reusable `reusable-main-guard.yml` and `python -m main_guard` turn red for any update to `main` by an actor not on the list in the caller's workflow file.

### Changed
- `jira-status-waived` joins the labels only Ahmed sets: `agent-gh` refuses to add or remove it. It lets a PR past elbayoumy's Jira status check, which counts it only when Ahmed added it (BASITA-168).
- An App pushes only to its own `branch_prefix` (`malek/`, `haya/`, `salma/`, `talia/`, `sherif/`, `adam/`, `nora/`, and `yousef/` for the docs App). Malek, Haya and Salma are now refused outside their prefix. `main`, `master` and `claude/*` stay refused for every App. Only `basita-sync` updates `main`, only of `basitasolutions/basita-ops`, never forced, and it pushes nothing else and runs no `gh` command. `basita-reader` reads only (BASITA-157).
- Yousef gets a `push_app`: `agent-git yousef push` and `agent-gh yousef pr create|edit` run as `basita-yousef-docs[bot]`; his reviews and comments keep his user token. agent-attribution maps `basita-yousef-docs[bot]` to Yousef, with no workflow change. `gh-check` compares each App with its own `permissions` (BASITA-157).
- `agent-git` knows `sync`, for `sync.sh` once it commits and pushes as `basita-sync` (BASITA-157).
- `agent-git <agent> commit` uses the App's bot address (`<bot id>+basita-<agent>[bot]@users.noreply.github.com`) for Malek, Haya and Salma, so GitHub links the commit to the bot. `AGENT_GIT_LEGACY_EMAIL=1` goes back to `<agent>@basitasolutions.com` (BASITA-149).
- agent-attribution maps the Apps' bots to their agents (from `config/github-identities.json`, so the reusable workflow is unchanged): a bot commit needs the matching `Agent:` trailer, and a pull request opened by a bot must not name another agent. These are warnings while `"attribution"` is `"warn"`, errors once it is `"fail"`. Other bots are still skipped (BASITA-149).

### Added (BASITA-161, T4 fired by GitHub events: playbook part)
- `python -m arch_review.fire`: the last step of Architecture review can start Yousef's review (`waiting`) or the author's rework (`changes`) through a pickup routine's `/fire` URL, with fixed fields only (`KIND REPO PR SHA TIER ROUND`), never the PR's text. The repo variable `T4_FIRE` switches it: `off` (default, the step doesn't run), `dry-run` (says what it would fire) or `on`. Loop guards of plan 10.10: only PRs opened by `t4.agent_bots` from the same repository (never forks); a review only when Yousef has no review on that commit; a rework only for changes requested on the head; at most `t4.max_rework_rounds` (2) reworks, then "round cap, waiting for <owner>"; one claim comment by github-actions[bot] so a (PR, commit, kind) fires once from GitHub. The routine tokens (`ROUTINE_FIRE_OPUS`, `ROUTINE_FIRE_SONNET`) are only in that step's environment and never printed; the step never changes the check's result. The workflow change is a patch for Ahmed (D4).
- `reviewer_own_bots` in `.github/review-rules.json` (`basita-yousef-docs[bot]`): on a PR one of them opened or committed to, Yousef's review can't count, and the check's new state `owner` waits for Ahmed.
- `templates/t4-routine-prompt.md`: the pickup routines' prompt for both payload forms, the review and rework briefs, and the design of `pickup-event.sh` (basita-ops part).

## 2026-11-02

### Added
- `standards/dfp-flow.md`: a picture of DFP's flow (statuses, arrows, who owns each step), generated from the `jira-flow` table by `python -m atl_agent.flowchart --write`. It says plainly that Jira enforces nothing yet. A test fails when the picture and the table differ, and one test per non-`jira-flow` step runs the real command and checks its from and to (BASITA-69).
- `python -m atl_agent.flowchart check`: read-only. Per status and work type, reads Jira's `GET /transitions` on one live issue. A status whose moves are all global prints "allows any move" and does not fail; once Jira is narrowed, any difference from the table exits 1. No issue means "not checked", never a pass (BASITA-69).

## 2026-11-01

### Added
- `atl <agent> jira-ready <KEY> --assignee NAME`: in DFP it names who gets the issue, and needs an unreleased Fix Version (BASITA-72).
- `@Name` in a Jira comment, description or `jira-flow` reason becomes a real mention (BASITA-88).
- `atl <agent> jira-create <PROJECT> <type> "summary" "description"`: any agent can create a signed Story, Task, Bug or Sub-task in BASITA, DFP or ELB; Nora and Sherif create Epics; Adam, Nora, Sherif and Salma can assign on create; `--link` adds `relates to`; a DFP Bug goes to Adam, unless Salma sends a clear technical one to an engineer (BASITA-41).
- `atl <agent> jira-flow <KEY> <action> "why"`: one table for DFP's moves between queues (submit, pass, fail, accept, reject), with the next assignee. Anything else is refused (BASITA-41).

### Changed
- Jira close on merge: `Closes DFP-x` moves an issue to `Released` only from `Ready for Release`, and otherwise comments the reason. BASITA and ELB are unchanged. The default account is Sherif's (BASITA-41).

## 2026-10-31

### Added
- `atl` reads and writes as the agent's own identity (BASITA-41): `jira-get <KEY>` (issue, comments, sub-tasks, linked issues), `jira-jql "<JQL>"`, `confluence-get <page id or URL>`, `confluence-search "<words or CQL>"`, `confluence-create <space key> "text" --title "Title"` and `confluence-edit <page id or URL> "text"`. Each read names the agent that read it. Nothing deletes. The Confluence commands need a scoped Confluence token for the agent.
- `--via <agent>` works through another agent's Atlassian user and signs `— Maya (Basita AI, via Sherif)`; `atl` and `agent-git` know Yousef and Basel (BASITA-41).

## 2026-10-29

### Added
- Jira close on merge: a PR whose description says `Closes <KEY>` moves that Jira issue to Done when it merges and comments with the PR links (reusable `reusable-jira-close.yml`, `python -m atl_agent.close`). Intermediate PRs say `Refs <KEY>`; every PR title and branch carries the key.
- `atl`: `jira-comment --mention <account id>` and a signed `— <Agent> (Basita AI)` line on every Jira comment; `jira-start <KEY>` (To Do to In Progress plus the `agent-picked` label, refusing a second pickup) for autonomous pickup.
- Agent attribution: `scripts/agent-git` commits as a named AI agent (own author/committer plus `Agent:` and `Requested-by:` trailers), and the reusable `agent-attribution` workflow checks every pull request for it, rejects Jira Smart Commit commands in agent commits, and warns on AI-made commits authored as a human and on missing issue keys. Standard: [06 — Agent identity](standards/06-agent-identity.md).
- `scripts/atl` (trial): Jira comments and Confluence page appends as a named agent, with that agent's scoped API token from the macOS Keychain.

## 2026-10-28

### Added
- Reusable Node CI: an optional `audit-in-build-job` input runs the dependency audit inside the "Build and test" job, so a private repository stops paying a separate billed minute for a 10-second job. Off by default; the audit still runs on every call and still fails the run.

## 2026-10-10

### Changed
- Moved every workflow to the Node 24 versions of the GitHub Actions (`checkout@v6`, `setup-python@v6`, `setup-node@v6`) ahead of the Node 20 deprecation ([#3](https://github.com/ahmedoraby-eng/engineering-playbook/pull/3)).
- The reusable Python CI workflow accepts several space-separated coverage packages ([#2](https://github.com/ahmedoraby-eng/engineering-playbook/pull/2)).

### Added
- `agent-evals`: a guarded agent runtime and an evaluation suite that runs in CI — tenant isolation, human confirmation for destructive and external actions, prompt-injection and PII cases, plus a test proving the evals fail when guardrails are switched off ([#2](https://github.com/ahmedoraby-eng/engineering-playbook/pull/2)).
- `ai-guard --warn-only`, so teams can trial the guardrails before enforcing them ([#1](https://github.com/ahmedoraby-eng/engineering-playbook/pull/1)).
- Initial playbook: standards for development, testing, shipping, observability and AI-assisted engineering; `ai-guard`; reusable Python and Node.js CI; engineering health scorecard; ADR, incident review and pull request templates.

### Process
- Pull requests are squash-merged with the pull request title as the commit message, and merged branches are deleted automatically.
