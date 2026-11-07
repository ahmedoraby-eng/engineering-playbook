# Wave plan: <release> wave <n>

- **Release / Fix Version:** <e.g. 0.2.2>
- **Wave:** <n>, planned <dd/mm/yyyy>, start <dd/mm/yyyy>
- **Owner:** Sherif (project management). Inputs from Adam (tickets), Malek (plan step), Yousef (hot-spot check).
- **Status:** draft | checked by Yousef | published (work may start)

A wave plan is written by Sherif before a wave starts and lives with the release plan
(for Elbayoumy: `basita-ops/work/elbayoumy/release-<version>/`). It has four parts. Parts 2 and 3
come from ADR-020 (parallel branches without shared-file conflicts, accepted by Ahmed 07/11/2026).

## 1. Tickets in the wave

| Order | Jira | Ticket | Owner | Size | Depends on | Register IDs |
|---|---|---|---|---|---|---|
| 1 | DFP-… | | | | | AA-… (reserved in the ledger BASITA-206) |

"Register IDs" are the AA/A ids reserved for the ticket (ADR-020 decision 2, BASITA-199). The ledger is
the Jira issue **BASITA-206**: one comment per id, `RESERVE AA-n -> KEY`, and the first comment wins. Adam
keeps it and the next free id. Copy the ids from BASITA-206 into this column; "none" when the ticket adds
no register entry. A ticket that adds an assumption with no reserved id is not ready for the wave, and Sherif
checks this column against BASITA-206 when compiling the plan.

## 2. File-touch map (ADR-020 decision 6, BASITA-201)

One row per ticket. It says which files and shared components the ticket is expected to touch, so
tickets that overlap on a hot spot are sequenced or merged into one PR **before** work starts, not
resolved as merge conflicts afterwards.

| Jira | Expected files / areas | Hot spots touched | Overlaps with | Decision |
|---|---|---|---|---|
| DFP-… | `src/modules/…/XPage.tsx`, i18n `x` | `ImportPage.tsx`, `configService.ts` | DFP-… (same hot spot) | stack on DFP-… / one PR with DFP-… / parallel (no overlap) |

**Column rules**
- *Expected files / areas*: paths or folders from Malek's plan step. A folder is fine when the ticket
  is broad (`src/modules/feeding/ui/*`). Say "new file" for files the ticket creates.
- *Hot spots touched*: only entries from the hot-spot list (section 3). Empty means none.
- *Overlaps with*: every other ticket in this wave **or still open from the last wave** that touches
  one of the same hot spots.
- *Decision*: one of the four below. Every overlap row has a decision before the wave is published.

**Decisions on an overlap** (pick the first that fits)
1. **Platform PR first.** If the overlap is a new or changed shared component (a grid, the page
   header, `ReasonButton`), that change lands first, alone, as a small platform PR (ADR-020 decision 5).
   The feature tickets use it. It is never copied into several PRs.
2. **Stack.** If one ticket needs the other's change, the second branches from the first
   (its base is the first PR's branch) and is ordered after it in the merge train.
3. **One PR, one owner.** If two small tickets change the same lines of a hot spot for related
   reasons, they go in one PR with one owner (Refs both keys; Closes both in the final PR).
4. **Parallel, ordered.** If they touch the same hot file in different places (different functions,
   different i18n keys in alphabetical order), they run in parallel and the merge train orders them.
   Use this only when Yousef agrees the places don't overlap.

Two tickets with **no shared hot spot** run in parallel with no decision needed.

**Who fills it, and when**

| Step | Who | When |
|---|---|---|
| Ticket list and Register IDs | Adam | when the tickets are Ready for Development |
| Expected files per ticket (part of the plan step) | Malek, from his implementation plan; a subagent may draft it | before the wave plan is published |
| Compile the map, find overlaps, propose a decision per overlap | Sherif | same day |
| Check the hot spots and the decisions, add any hot spot missed | Yousef | before the wave starts |
| Merge-train order (section 4) follows from the decisions | Sherif publishes, Talia runs it | when the wave starts |

**Rules while the wave runs**
- Work starts only on a published map. A ticket added mid-wave gets its row (and a decision) before
  its branch is cut.
- If a PR touches a hot spot that its row does not list, Malek says so in the PR and in the ticket;
  Sherif updates the map and the train order the same day.
- At the end of the wave, Sherif adds a line under the map: hot spots that conflicted anyway, and any
  new hot spot for section 3. ADR-020 follow-up 6 measures the first 0.2.2 wave against this.

## 3. Hot-spot list

The files and shared components where parallel PRs collide. Start from the list kept with the release
plan (for Elbayoumy: `basita-ops/work/elbayoumy/hot-spots.md`) and update it at the end of each wave.
The CI hub list of ADR-020 enforcement E4 (Basel) is the subset of these that need the `hub-change`
label.

## 4. Merge-train order (ADR-020 decision 3)

| # | PR | Jira | Base (main or the PR it stacks on) | Why here |
|---|---|---|---|---|

After each merge only the next PR is brought up to date. Talia runs the train; for the app repo the
order is also written to `.github/merge-train.json` once the merge-train workflow exists (ADR-020 E1).
