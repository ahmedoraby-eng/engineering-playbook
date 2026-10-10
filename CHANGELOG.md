# Changelog

Notable changes to the playbook, newest first. Each entry links to the pull request that made it.

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
