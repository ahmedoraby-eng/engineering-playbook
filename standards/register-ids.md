# Register ids: reserved when the ticket is written

ADR-020 decision 2 (accepted by Ahmed, 07/11/2026), BASITA-199. Parallel PRs used to pick "the next AA" themselves, so two PRs took the same id (AA-125…128) and had to renumber across registers and tests. Now an id is reserved once, when the ticket is written, and a PR only uses ids its ticket holds.

## The ledger

**Jira issue [BASITA-206](https://basitasolutions.atlassian.net/browse/BASITA-206)**: append-only comments, one reservation per comment. It stays open (label `agent-hold`) and is never picked up. Jira gives every comment a timestamp, so when two people reserve at the same moment, the earlier comment wins. Every agent already reaches it with `atl`, from the Mac or the cloud. No file to merge, no field to configure.

Comment forms (one line, exactly):

```
RESERVE AA-173, AA-174 -> DFP-147
RESERVE A-22 -> DFP-151
VOID AA-175 -> DFP-150   (lost a race, or the reservation was a mistake)
```

## Reserving (whoever writes the ticket: Adam, Maya through Sherif, Sherif, Talia)

1. `atl <agent> jira-get BASITA-206 --json` (every comment; the Markdown snapshot shows only the last 10) and take the highest AA and the highest A ever written in its comments (`BASELINE`, `RESERVE` or `VOID`). Add 1. Pad A to two digits (`A-22`).
2. `atl <agent> jira-comment BASITA-206 "RESERVE AA-<n> -> <KEY>"`.
3. Read BASITA-206 again. If an **earlier** comment holds any of your ids, post `VOID` for yours and reserve the next free ids (step 1 again).
4. Put the line `Register IDs: AA-<n>, ...` in the ticket: in the description when you create it, or as a comment on an existing ticket. A ticket that needs none says `Register IDs: none`.

Reserve only for stories that will add an assumption. One id is enough when unsure; more is cheap.

## Rules

- **Never reused.** An id that isn't used stays burned. The counter only goes up.
- **Malek never invents an id.** The PR adds only the ids in its ticket's `Register IDs` line. If it needs one more, he reserves it in BASITA-206 (same steps) and adds the line to the ticket as a comment, or asks Adam on the ticket. He never takes "the next number" from `main`.
- **Amending** an existing entry needs no reservation; the id already exists.
- **Checks.** The CI uniqueness test (ADR-020 decision 1) fails on a duplicate id. The Jira status check (ADR-020 E2, Basel) goes red when a PR adds an AA/A id that isn't in its ticket's `Register IDs` line or that already exists on `main`. It reads the description and the comments.
