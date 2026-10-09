# Shipping standard

**Goal:** shipping is routine, frequent and boring — and any change can be undone in minutes.

## Must

| # | Practice | Why |
|---|---|---|
| S1 | One automated pipeline from merge to production; no manual build steps | Manual steps are where releases go wrong |
| S2 | The same artifact is promoted through environments; configuration differs, code does not | What you tested is what you ship |
| S3 | Zero-downtime deployments (rolling, blue-green or canary) for customer-facing services | Deploys shouldn't need a maintenance window |
| S4 | Risky changes ship behind feature flags, released gradually and removed after rollout | Separates deploying code from releasing behaviour |
| S5 | A documented rollback that has been rehearsed | The first rollback should not happen during an incident |
| S6 | Database migrations are backwards-compatible (expand → migrate → contract) | Lets old and new code run side by side during rollout |

## Measures (DORA)

Track per team, review monthly, and compare a team with its own past — not with other teams.

| Measure | Healthy direction |
|---|---|
| Deployment frequency | Daily or more for active services |
| Lead time for changes (commit → production) | Under a day |
| Change failure rate | Under 15% |
| Time to restore service | Under an hour |

## Should

- Canary analysis that compares error rate and latency automatically before full rollout.
- Release notes generated from merged PR titles.
