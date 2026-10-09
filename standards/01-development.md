# Development standard

**Goal:** any engineer can pick up any repository and make a safe change in their first week.

## Must

| # | Practice | Why |
|---|---|---|
| D1 | Trunk-based development: short-lived branches (≤ 2 days) merged to `main` through pull requests | Small batches are easier to review, test and roll back |
| D2 | Every PR has one approving review and green CI before merge; no direct pushes to `main` | Two sets of eyes and an automated bar on every change |
| D3 | PRs are small: aim for under 400 changed lines, one concern each | Review quality falls sharply on large diffs |
| D4 | A README that gets a new engineer from clone to running tests in under 30 minutes | Onboarding cost is paid on every hire and every deployment |
| D5 | Significant decisions recorded as ADRs (`templates/adr.md`) | Teams change; the reasoning must survive them |
| D6 | Secrets only in a secrets manager or CI secrets — never in code, tickets or prompts | Leaked credentials are the most common, most preventable incident |
| D7 | Formatter and linter enforced in CI, not left to review | Reviewers should spend attention on design, not whitespace |

## Should

- Conventional, imperative commit messages that explain *why*.
- Code owners for sensitive areas (auth, billing, data deletion, infrastructure).
- Dependency updates automated (Dependabot or Renovate) and merged weekly.

## Evidence a reviewer can check

Branch protection on `main`, PR history with reviews, CI configuration, `docs/adr/`, time-to-first-PR for the last three joiners.
