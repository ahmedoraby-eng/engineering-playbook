# Changelog

Notable changes to the playbook, newest first. Each entry links to the pull request that made it.

## 2026-10-31

### Added
- `atl` reads and writes as the agent's own identity (BASITA-41): `jira-get <KEY>` (issue, comments, sub-tasks, linked issues), `jira-jql "<JQL>"`, `confluence-get <page id or URL>`, `confluence-search "<words or CQL>"`, `confluence-create <space key> "text" --title "Title"` and `confluence-edit <page id or URL> "text"`. Each read names the agent that read it. Nothing deletes. The Confluence commands need a scoped Confluence token for the agent.

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
