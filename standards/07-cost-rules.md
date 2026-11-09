# 07 — Cost rules

**Goal:** every repository and every consumption area follows the same cost rules, written once here. A new repository, workflow, routine or agent job follows them from its first day; nobody re-decides them per repository.

Decided by Ahmed, 09/11/2026 (BASITA-224): "we need to cover every area that can relate to consumption ... whether the cost comes from the project, the client's changes, the platform or Basita's operations work, this should be generalised". Background: the Actions rules of 07/11 (BASITA-194, Yousef's decision §7) reached the client repository only; basita-ops was missed and used 90% of the November Actions budget (BASITA-223).

**Where they are checked**
- **Register:** `basita-ops/work/basita/github-actions/coverage.md` lists every repository, every workflow and every other consumption area, with ✅/❌ against the rules below and an owner for each gap.
- **Standing check:** `basita-ops/actions-coverage-check.sh`, once a day from the hourly check. It flags a repository or workflow missing from the register, and C1–C3 below. Its mode is `ACTIONS_COVERAGE_MODE` in `basita-ops/jira-agents.conf`.
- **Monthly figures:** `basita-ops/actions-minutes.py` gives Actions minutes and gross dollars per repository and workflow for Sherif's cost log.

**Exceptions.** A rule is broken only with a row in the register's exceptions table: the repository, the workflow, the rule, the reason, and who approved it (Yousef for workflow design, Ahmed for a guard's behaviour or for spend). The check honours that row; nothing else silences it.

## A. GitHub Actions

Prices (read 07/11/2026; recheck in the monthly review): Linux 1-core `ubuntu-slim` $0.002/min, Linux 2-core `ubuntu-latest` $0.006, Linux arm64 2-core $0.005, Windows $0.010, macOS $0.062. Public repositories run Actions for free; a reusable workflow is billed to the repository that calls it.

| # | Rule | Why |
|---|---|---|
| C1 | **Every job runs on `ubuntu-slim`**, unless it builds, tests or runs a browser and needs 2 cores or more than 5 GB of RAM (e.g. CI build and test, E2E). Such a job is an exception row. Slim jobs time out at 15 minutes. | A guard job takes 10–30 s; on Slim it costs a third. |
| C2 | **Workflows on `push`, `pull_request` or `pull_request_target` have path filters** (`paths:` or `paths-ignore:`), so docs-only and records-only changes (`docs/**`, `work/**`, `**/*.md`) start nothing. A guard that must see every change (Main guard, a security or tenant-leak check) is an exception row. | Most pushes in our repositories change records, not code. |
| C3 | **Every workflow on a repeatable event has a `concurrency:` group**, per PR or per ref, with `cancel-in-progress: true` when a newer run makes the older one useless. Not needed for `workflow_call` (the caller sets it), `workflow_dispatch` or `schedule`. A run that must never be cancelled (one per merged PR, one per push to main) is an exception row. | Four pushes in a minute should cost one run. |
| C4 | **Commits by bots and by `sync.sh` start no workflow** unless they touch a path the workflow protects: a job `if:` on the actor, or path filters that leave out what they change. A skipped job is free. | `sync.sh` pushes main of basita-ops about 30 times a day. |
| C5 | **Every job is billed at least one whole minute.** Prefer one job with several steps over several tiny jobs or workflows on the same event; don't add a workflow for a check that fits in an existing job's step. | 5 guard jobs of 12 s on each PR push are billed 5 minutes. |
| C6 | **Long suites run once, when they are useful:** E2E on a PR only when the `run-e2e` label is added (plus `workflow_dispatch`), and nightly on main; not on every push. Cache what is downloaded (npm, Playwright browsers). | E2E was 51% of the minutes on 01–05/11. |
| C7 | **Triggers are only the events the job reads.** No `edited`, `labeled` or `unlabeled` unless the check depends on the title, body or a label. | Each extra event is another billed minute. |
| C8 | **No self-hosted runner on a team member's Mac.** A runner elsewhere needs Yousef's design and Ahmed's approval. | Security (PR code next to Keychain secrets) and availability. |

A new workflow, or a change to one, states in its PR which of C1–C7 it meets and names any exception row. A new repository is added to the register before its first workflow merges.

## B. Claude tokens

| # | Rule | Why |
|---|---|---|
| T1 | **Model by complexity** (operating model §2.2): Opus for reasoning, planning, architecture, protected calculations and cost work; Sonnet for ordinary feature code, tickets, reviews and test evidence; Haiku only for purely mechanical work. The latest model of each family. | The same result for less. |
| T2 | **Default model per role** (Ahmed, 08/11/2026): Yousef, Malek, Adam and Talia on Opus; Nora, Maya, Sherif, Haya and Basel on Sonnet with medium effort; Salma on Sonnet with high effort. A hard or blocking task goes up (`model-opus` label). Routines, pickup and leads use the same table: each routine's model is written in `basita-ops/setup/routines/models.conf`; pickup's defaults are `AGENT_DEFAULT_MODELS` and `AGENT_DEFAULT_EFFORTS` in `jira-agents.conf`. | One table, no drift between where an agent runs. |
| T3 | **Effort by complexity**: `low` or `medium` for mechanical and routine stages, `high` for implementation and review, `xhigh`/`max` only when a lead asks for it on a critical task. | Effort multiplies tokens. |
| T4 | **Every autonomous session is logged** with date, character, task, model, effort and category (`work/talia/agent-sessions-log.md`), so Sherif can price it. | What isn't logged can't be priced. |
| T5 | **Renew a session at about 300k tokens** with a handover file; archive finished sessions; no idle polling loops (a routine that fires with nothing to do is stopped by its own guard). | Long contexts cost on every turn. |
| T6 | **Read the narrowest source**: a ticket from its Markdown snapshot (`work/jira/<KEY>.md`), files by line range, logs by `tail`; no repeated full reads of large files. | Most tokens are input. |

## C. The Mac

| # | Rule | Why |
|---|---|---|
| M1 | **At most `MAX_LIVE_SESSIONS` working sessions** (9 today), pickup within its own cap; lower it when swap stays above 4 GB. | Swap slows every session. |
| M2 | **Worktrees are removed when their work is on main** (`mac-janitor.sh`); one worktree per task, under the session's scratchpad or `~/Code/<repo>-wt-<KEY>`; a lead removes its own at the end. | Each app worktree carries its own `node_modules` (~150 MB) and build output; 140 were kept on 09/11. |
| M3 | **Test runs and dev servers end with the task**; the janitor stops a test run after `JANITOR_TEST_MAX_MINUTES` and a dev server after `JANITOR_DEV_MAX_HOURS`. One E2E run at a time per Mac, on its own port. | A stray Playwright run pins cores for hours. |
| M4 | **Attention line when free disk is below `ALERT_DISK_FREE_GB` or swap above `ALERT_SWAP_GB`.** | The Mac is the team's only machine. |

## D. Preview deploys (Cloudflare Workers Builds)

| # | Rule | Why |
|---|---|---|
| P1 | **Previews only for branches someone will open**: limit non-production branch builds (Workers Builds branch control) to the branches under review, and set build watch paths so docs and records changes don't build. | Workers Builds builds every pushed branch by default. |
| P2 | **Every preview stays behind Cloudflare Access** ("All traffic"); a preview URL is never shared outside Basita. | The bundle holds client data. |
| P3 | **Build minutes are read monthly** from the Cloudflare dashboard (Workers → Builds) and logged with the Actions figures. | The free tier has a monthly build-minute limit. |

## Review

The monthly automation review rechecks the prices, the rules and the register against what ran. A rule change is a PR to this file; the register and the check follow it.
