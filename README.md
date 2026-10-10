# Engineering Playbook

[![CI](https://github.com/ahmedoraby-eng/engineering-playbook/actions/workflows/ci.yml/badge.svg)](https://github.com/ahmedoraby-eng/engineering-playbook/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Shared engineering standards for running **many teams across many codebases**, with the automation that enforces them — including guardrails for **AI-assisted engineering**.

The standards are short on purpose: each one is a handful of *must* practices, why they matter, and the evidence a reviewer can check. Where a standard can be enforced by a machine, this repository ships the tool or the reusable workflow that does it.

## What's inside

| Area | Contents |
|---|---|
| [Standards](standards/) | [Development](standards/01-development.md) · [Testing](standards/02-testing.md) · [Shipping](standards/03-shipping.md) · [Observability](standards/04-observability.md) · [AI engineering guardrails](standards/05-ai-engineering-guardrails.md) |
| [`ai-guard`](src/ai_guard/) | A pull-request check that enforces the AI guardrails: disclosure, no secrets, tests with AI-written code, human review for sensitive areas, reviewable size |
| [`agent-evals`](src/agent_evals/) | A guarded agent runtime and an [evaluation suite](evals/support_agent.json) run in CI: tenant isolation, human confirmation for destructive and external actions, prompt-injection and PII cases |
| [Reusable CI](.github/workflows/) | Drop-in GitHub Actions workflows for [Python](.github/workflows/reusable-python-ci.yml) and [Node.js](.github/workflows/reusable-node-ci.yml) services, and [`ai-guard`](.github/workflows/reusable-ai-guard.yml) for any repository |
| [Assessment](assessments/engineering-health-scorecard.md) | A one-day engineering health scorecard for a team or company, ending in the three highest-leverage changes |
| [Templates](templates/) | [ADR](templates/adr.md) · [Incident review](templates/postmortem.md) · [Pull request](.github/pull_request_template.md) |

## How the pieces fit

```text
 Standards (what good looks like)
      │
      ├── Reusable CI ─────────── lint · tests · coverage gate · dependency audit
      ├── ai-guard ────────────── AI disclosure · secrets · tests · sensitive paths
      ├── agent-evals ─────────── eval suite gating every change to an AI feature
      ├── PR / ADR / incident templates
      │
      └── Health scorecard ────── measure a team against the standards,
                                   pick the 3 changes that matter most
```

## ai-guard

`ai-guard` reads a pull request's diff, description and labels and reports findings as annotations on the PR.

| Rule | Severity | What it checks |
|---|---|---|
| `ai-disclosure` | error | Exactly one AI-assistance box is ticked in the PR description |
| `no-secrets` | error | No keys, tokens, private keys or hard-coded credentials in added lines |
| `tests-with-ai-code` | error | AI-assisted changes to source code include test changes |
| `protected-path` | error | AI-assisted changes to auth, permissions, migrations or CI carry a `human-reviewed` label |
| `reviewable-size` | warning | AI-assisted PRs stay small enough to review properly |

Run it locally:

```bash
git diff main...HEAD > changes.diff
PYTHONPATH=src python -m ai_guard --diff changes.diff --pr-body pr.md --labels human-reviewed
```

Add `--warn-only` to report findings without failing the build — useful for a trial period before enforcing.

Each repository tunes it with an optional [`.ai-guard.toml`](.ai-guard.toml) (source and test paths, sensitive areas, size guideline, extra secret patterns). The [`ai-guard` workflow](.github/workflows/ai-guard.yml) runs it on every pull request — including this repository's own.

## agent-evals

A worked example of AI guardrails B1–B4 and B7 for an agent. A small helpdesk agent proposes actions through a planner (normally an LLM); the **runtime** decides what actually happens:

- only allow-listed tools run, and records are scoped to the caller's tenant
- destructive (`close_ticket`) and external (`send_email`) actions wait for human confirmation
- actions prompted by retrieved documents are dropped — prompt injection is contained, not just detected
- card-like numbers are redacted before any text reaches the model
- answers cite their source, and the agent says "I don't know" instead of guessing

The planner in CI is a deliberately naive rule-based stand-in (it obeys instructions it finds in documents), so the suite proves the runtime contains a misbehaving model without needing an API key. A test also runs the suite with guardrails switched off and asserts the critical cases fail — the evals catch regressions rather than passing by construction.

```bash
PYTHONPATH=src python -m agent_evals evals/support_agent.json --min-pass-rate 1.0
```

CI runs the suite on every change and publishes the results table to the run summary.

## Adopted by

| Repository | Uses |
|---|---|
| [dairy-farm-management-platform](https://github.com/ahmedoraby-eng/dairy-farm-management-platform) | Reusable Python CI · `ai-guard` with a tenancy-aware policy |

A repository adopts the playbook with two short workflow files — see [its CI](https://github.com/ahmedoraby-eng/dairy-farm-management-platform/blob/main/.github/workflows/ci.yml) and [ai-guard](https://github.com/ahmedoraby-eng/dairy-farm-management-platform/blob/main/.github/workflows/ai-guard.yml). Changing a standard here changes it everywhere it is adopted.

## Adopting the playbook

1. **Assess** the team with the [health scorecard](assessments/engineering-health-scorecard.md) and agree on three changes.
2. **Automate** the gates: call the reusable CI workflow and add the `ai-guard` workflow — with `--warn-only` for two weeks, then enforced.
3. **Review** progress every two weeks with the team's technical lead; revisit the standards quarterly and delete rules that don't prevent real problems.

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest --cov=ai_guard --cov=agent_evals
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Maintained by [Ahmed Oraby](https://www.linkedin.com/in/ahmedoraby/).

## License

MIT
