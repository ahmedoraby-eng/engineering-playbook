# Engineering health scorecard

A one-day assessment of a team or company's engineering practice, used when an engineer joins a new team or when planning where to invest. Score each area 1–4 from evidence, not interviews alone.

| Score | Meaning |
|---|---|
| 1 | Absent or ad hoc |
| 2 | Exists but inconsistent, or depends on individuals |
| 3 | Consistent and automated for most work |
| 4 | Measured and continuously improved |

## Areas

| Area | Evidence to look at | Standard |
|---|---|---|
| Code review and branching | Branch protection, PR sizes, review turnaround, % of changes reviewed | [Development](../standards/01-development.md) |
| Automated testing | What CI runs, failure rate, flaky tests, time to green | [Testing](../standards/02-testing.md) |
| Delivery pipeline | Deploy frequency, lead time, manual steps, rollback history | [Shipping](../standards/03-shipping.md) |
| Operability | Dashboards, SLOs, on-call load, last three incident reviews | [Observability](../standards/04-observability.md) |
| Security hygiene | Secret handling, dependency audit, access reviews | [Development](../standards/01-development.md) |
| AI-assisted engineering | Tools in use, data terms, review practice, evals for AI features | [AI guardrails](../standards/05-ai-engineering-guardrails.md) |
| Architecture and docs | ADRs, onboarding time, ownership map | [Development](../standards/01-development.md) |
| Team health | Attrition, hiring bar, growth paths, knowledge concentration | — |

## Output

1. A score per area with the evidence behind it.
2. The **three** changes with the best impact-to-effort ratio for the next quarter — not a list of everything that's wrong.
3. An owner and a measurable target for each, reviewed with the team's technical lead every two weeks.
