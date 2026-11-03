# Changelog

Notable changes to the playbook, newest first. Each entry links to the pull request that made it.

## 2026-11-03

### Added
- GitHub identities for the agents that write code (BASITA-149). `config/github-identities.json` lists, without secrets, the Apps of Malek, Haya and Salma (App id, installations, bot id) and Yousef's user. `scripts/agent-gh <agent> <gh args>` runs gh with a token for that command only: an App's 1-hour installation token for the one repository, read-only for reads and `pull_requests:write` with `contents:read` for pull request writes, so it cannot merge or push. Yousef's token is chosen by the repository owner. It refuses merging, closing, deletes, settings, secrets, workflows, `gh auth`, App approvals and the labels only Ahmed sets.
- `scripts/agent-git <agent> push`: pushes with the App's `contents:write` token through a one-command credential helper that replaces the Mac's own login. Never to main or master, never forced, deleting, bulk or tags.
- `scripts/gh-check [agent]`: read-only. Login, App permissions, bot id, installations (an unlisted one is an attention line), the repos each reaches, and Yousef's token expiry (attention 14 days before). Never prints a token.
- `scripts/gh-store <agent>`: stores an App key or one of Yousef's tokens in the Keychain, only if it has the right shape.
- After Yousef's review: an App's token is revoked as soon as its command ends, even after a failure; the git credential helper answers only `https://github.com`; a remote needs exactly one push URL; submodules are never pushed; trace and `GIT_CONFIG_*` variables are cleared; Yousef gets reads, reviews and comments only; `gh-check` checks the webhook is off, pages installations, and fails if Yousef has push anywhere; `gh-store` passes the secret on stdin (BASITA-149).
- Architecture review (BASITA-150): a soft check that a PR carries the architect's review of its latest commit. `python -m arch_review` classifies the PR from `.github/review-rules.json` read from the base branch (not required, waived by the owner's `review-waived` label, critical = `max`, large = `xhigh`, normal = `high`), requests the reviewer itself, and stays red until the reviewer's latest review approves the head commit. An approval of an older commit carries when the change set is identical. The repo variable `ARCH_REVIEW_MODE` (`off`, `report`, `on`; unset means `report`) decides the effect. Reusable `reusable-architecture-review.yml` on `pull_request_target`, plus `reusable-architecture-review-rerun.yml`, which only re-runs it when the reviewer reviews. On this repo it waits for the review instead of requesting it (plan section 4.1a).
- Main guard (BASITA-150): reusable `reusable-main-guard.yml` and `python -m main_guard` turn red for any update to `main` by an actor not on the list in the caller's workflow file.

### Changed
- `agent-git <agent> commit` uses the App's bot address (`<bot id>+basita-<agent>[bot]@users.noreply.github.com`) for Malek, Haya and Salma, so GitHub links the commit to the bot. `AGENT_GIT_LEGACY_EMAIL=1` goes back to `<agent>@basitasolutions.com` (BASITA-149).
- agent-attribution maps the Apps' bots to their agents (from `config/github-identities.json`, so the reusable workflow is unchanged): a bot commit needs the matching `Agent:` trailer, and a pull request opened by a bot must not name another agent. These are warnings while `"attribution"` is `"warn"`, errors once it is `"fail"`. Other bots are still skipped (BASITA-149).

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
