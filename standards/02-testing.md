# Testing standard

**Goal:** a green build means the change is safe to ship.

## Must

| # | Practice | Why |
|---|---|---|
| T1 | Tests run on every PR and block merge when they fail | A test that doesn't gate anything gets ignored |
| T2 | Business rules covered by fast, deterministic unit tests | They change most often and break most quietly |
| T3 | At least one end-to-end path per critical user journey, run against a real build | Proves the pieces work together, not just alone |
| T4 | Tests never depend on the wall clock, network or test order; time is injected or fixed | Flaky tests destroy trust in the whole suite |
| T5 | Security-relevant behaviour has negative tests ("user A *cannot* read B's data") | Access bugs hide in the paths nobody tries |
| T6 | Bugs get a failing test before the fix | The same bug should never ship twice |

## Should

- A coverage floor on core domain packages (typically 80–90%), used as a ratchet, not a target.
- Contract tests at service boundaries owned by different teams.
- Flaky tests quarantined within a day and fixed within a week.

## Anti-patterns

- Coverage targets on glue code that drive low-value tests.
- Mocks so deep the test only proves the mock works.
- A separate QA phase as the main safety net.
