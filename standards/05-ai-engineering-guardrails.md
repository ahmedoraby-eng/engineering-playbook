# AI engineering guardrails

**Goal:** get the speed of AI-assisted engineering without lowering the quality, security or accountability bar.

AI is a tool used by an accountable engineer, not an author. These guardrails apply to code written with assistants and to features built on models and agents.

## 1. Using AI to write code

| # | Guardrail | Enforced by |
|---|---|---|
| A1 | The PR author owns every line, and must be able to explain it in review | Code review |
| A2 | AI assistance is disclosed in the PR (a checkbox, not a confession) | `ai-guard` — `ai-disclosure` |
| A3 | AI-assisted changes to production code come with tests | `ai-guard` — `tests-with-ai-code` |
| A4 | AI-assisted changes to sensitive areas (auth, permissions, migrations, CI) need an explicit line-by-line human review | `ai-guard` — `protected-path` + `human-reviewed` label |
| A5 | No secrets, customer data or proprietary code from other clients in prompts; use approved tools with no-training data terms | Tool allow-list, training |
| A6 | Generated diffs stay reviewable (~400 lines) | `ai-guard` — `reviewable-size` warning |
| A7 | New dependencies suggested by an assistant are verified to exist and be maintained before use | Review, dependency audit |

## 2. Building AI features and agents

| # | Guardrail |
|---|---|
| B1 | **Evals before launch.** Every model-backed feature has a versioned evaluation set (golden cases plus known failure cases) run in CI on prompt or model changes. |
| B2 | **Least privilege for agents.** Tools are allow-listed, scoped per tenant and per user, and rate-limited. Destructive or external actions (payments, emails, deletions) need human confirmation. |
| B3 | **Untrusted input stays untrusted.** Content retrieved from documents, web pages or users is data, never instructions; prompt-injection cases are part of the eval set. |
| B4 | **Grounded and attributable.** Answers that state facts cite their source; the system can say "I don't know". |
| B5 | **Observable.** Model calls are traced (see [observability](04-observability.md)) with cost and latency budgets per feature. |
| B6 | **Human fallback.** Every AI-driven flow has a non-AI path and a kill switch (feature flag). |
| B7 | **Data protection.** Personal data is minimised and redacted before it reaches a model; retention is documented per provider. |

A worked example of B1–B4 and B7 lives in [`agent-evals`](../src/agent_evals/): a guarded agent runtime with an evaluation suite that runs in CI.

## 3. Adoption

Roll out in this order, one portfolio team at a time: approved tools and A5 training → `ai-guard` with `--warn-only` for two weeks → enforce → evals (B1) for the first AI feature. Review the guardrails each quarter against incidents and near misses; remove any rule that isn't preventing real problems.
