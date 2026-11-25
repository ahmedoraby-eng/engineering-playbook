# ADR-024: Project baseline — one registry, one bootstrap, a daily conformance check

- **Status:** proposed (for Ahmed)
- **Date:** 2026-11-25
- **Deciders:** Ahmed (decision); Yousef (design); Basel and Malek (implementation)
- **Ticket:** BASITA-TBD

## Context

Ahmed, 25/11/2026: every project should get the same checks, CI/CD, continuous-deployment setup, the Jira↔GitHub link, the PR template, pickup/closes and the AI guard, and they should stay enforced.

What happened: the SLM repo shipped 21 PRs with none of DFP's setup. Each gap was found and patched by hand (playbook #76, SLM #22/#24, basita-ops #120/#121). The playbook's reusable workflows are **opt-in per repo**, so nothing was inherited.

Root cause, from the code. There is no single list of projects. Each tool keeps its own hard-coded list:

| Where | List |
|---|---|
| playbook `src/atl_agent/close.py` | `PROJECTS`, `REFS_PROJECTS` |
| playbook `src/atl_agent/client.py` | `CREATE_PROJECTS`, `DONE_PROJECTS`, `WONTDO_PROJECTS`, `PR_REPOS` |
| playbook `config/github-identities.json` | the App installations per owner/repo |
| basita-ops `jira-agents.conf` | `PROJECT_RANKS`, `PROJECT_DIR_*`, `SIMPLE_PROJECTS`, `TESTING_PROJECTS`, `JANITOR_REPOS` |
| basita-ops `jira-attention.sh` | `PROJECTS`, `REPOS` |

So a new project is missed by every list it isn't added to by hand.

Constraints:
- **Cost.** The Actions hard stop is $45/month. Heavy checks run on the Mac (§4.3b), and anything n8n can schedule goes on n8n.
- **GitHub plans (checked 25/11):**
  - `basitasolutions` is on GitHub Free with private repos. Rulesets and branch protection return HTTP 403 ("Upgrade to GitHub Pro or make this repository public"), so GitHub itself can block nothing there.
  - `ahmedoraby-eng` repos are public, so repo rulesets are free; the playbook already has `protect-main`.
  - Organisation rulesets, including "require workflows", come with GitHub Team ([changelog, June 2025](https://github.blog/changelog/2025-06-16-organization-rulesets-now-available-for-github-team-plans/), [about rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)).
- **Two owners.** Personal-account repos never get org rules.
- **No Basita name in SLM product content** (code, UI, README, docs). The registry and the checks live in Basita's playbook, not in the SLM repo.
- **Agent permissions.** The agent Apps have contents and pull-request rights only, with no `administration`. Repo settings, rulesets, secrets and App installs therefore stay human steps unless we decide otherwise.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| A. Status quo: opt in per repo by hand | Nothing to build | This is how SLM happened. No drift detection. |
| B. A GitHub template repo | New repos start complete | Applies only at creation. Doesn't cover Jira, Cloudflare or the Mac. Copies workflows, so they drift. Needs one template per owner. |
| C. GitHub Team + org ruleset requiring workflows | Real server-side blocking on org repos | ~$4/user/month. Org only, so the personal repos (SLM, playbook) are uncovered. Blind to Jira, Cloudflare and the Mac. |
| D. Third-party policy bot (e.g. OpenSSF Allstar) | Free, opens issues on drift | GitHub only. Another App with admin read. Doesn't know our Jira or deploy rules. |
| **E. Registry + baseline definition + bootstrap + daily conformance check + a light per-PR job, required by rulesets where free** | Covers GitHub, Jira, Cloudflare and the Mac in one place. Removes the hard-coded lists. Near-zero Actions cost. Works for both owners. | We build and maintain a small checker. On private org repos it detects and does not block, until C. |

## Decision

Option E, built in four parts. Option C stays as a later, separate decision for Ahmed (below).

### 1. Source of truth

| What | Where | Holds |
|---|---|---|
| **Registry** | playbook `config/projects.json` | One entry per project, with only identifiers that are already public in the playbook: `key` (Jira), `name`, `profile`, `jira_flow` (`simple` / `full`; close-to status), `repos[]` (`owner/name`, `role`: app/ops/docs), `deploy` (`none` or `{kind: "workers-builds", check: "Workers Builds: <worker>"}`), `brand` (`basita` / `none`), `cost_category`, `pickup` (`on` / `off`, rank), and `waivers[]` (`{item, reason, until, approved_by}`). |
| **Private overlay** | basita-ops `config/projects.local.json` | What must not sit in a public repo: health URLs, Cloudflare account ids, and Mac dirs that don't follow `~/Code/<repo>`. |
| **Baseline definition** | playbook `config/baseline.json` (versioned, `baseline_version: 1`) | The required items per profile. Each item has a check type and a fix hint. |
| **Templates** | playbook `templates/baseline/<profile>/` | Caller workflows (thin files that call the playbook's reusable workflows), the PR template, `.ai-guard.toml`, `review-rules.json` and CODEOWNERS. For `brand: none` they render with no Basita text. |

**Every tool reads the registry** instead of its own list: `atl` (all the `*_PROJECTS` tuples and `PR_REPOS`), `jira-attention.sh`, `jira-pickup.sh` (ranks and dirs), the janitor and `playbook-sync`. Adding a project then means adding one registry entry. A unit test fails if a Jira key or repo name is hard-coded outside the registry.

### 2. The baseline (version 1)

Profiles:
- `product`: a client app (DFP repo).
- `personal`: SLM. Same as `product`, except `brand: none`, its own Jira project and personal cost.
- `ops`: basita-ops and the playbook. No deploy and no coverage gate.
- `jira-only`: ELB, a Jira project with no repo of its own.

✓ = required for that profile; – = not required. The check column says how the checker tests it.

| # | Item | product / personal | ops | jira-only | How it is checked | Set up by |
|---|---|---|---|---|---|---|
| R1 | Listed in the registry | ✓ | ✓ | ✓ | – | agent (PR) |
| G1 | `ci` caller of `reusable-node-ci` / `reusable-python-ci`: lint+format, tests, **coverage ≥ 90 %**, dependency audit | ✓ | ✓ python / shell lint | – | file exists and `uses:` points at the playbook reusable, not a local copy | agent (bootstrap PR) |
| G2 | `ai-guard` caller + `.ai-guard.toml` | ✓ | ✓ | – | file + `uses:` | agent |
| G3 | `jira-close` caller, with `deploy-check` for deployable repos | ✓ | ✓ | – | file + `uses:`; last run on main not failing | agent |
| G4 | `main-guard`, `architecture-review` (+ rerun), `agent-attribution` callers | ✓ | ✓ | – | file + `uses:` | agent |
| G5 | `baseline` caller (new, §4) | ✓ | ✓ | – | file + `uses:` | agent |
| G6 | `.github/pull_request_template.md` with the AI-disclosure box and the `Refs`/`Closes <KEY>` line | ✓ | ✓ | – | markers present | agent |
| G7 | `review-rules.json`, CODEOWNERS | ✓ | ✓ | – | file exists | agent |
| G7b | Decision register from day one: `docs/decisions.md` (one table: ID, title, date, status, link) + `docs/adr/NNNN-title.md` (MADR-lite), linked from the README | ✓ | ✓ | – | files exist; README links the register | agent (bootstrap renders an empty register + ADR template) |
| G7c | **Review conversation rule** (Ahmed, 25/11): an `AGENTS.md` (and a `CLAUDE.md` that points to it) in the repo. It says: every review finding is numbered (F1, F2…); the author replies to each one in the PR (fixed + commit, or why not); a re-review marks each finding resolved or open; and the same applies to Jira ticket comments | ✓ | ✓ | – | the file exists and contains the rule's marker line | agent: part of the **project template** (Ahmed, 25/11). `baseline bootstrap` renders `AGENTS.md` + `CLAUDE.md` on day one with the shared rules: identity and commits via `agent-git`; `Refs`/`Closes <KEY>`; the review-conversation rule; read tickets from the snapshot; no merges by agents; and, for `brand: none`, the forbidden names. The check fails any repo without them |
| G7d | **Reply enforcement:** on a PR with review findings, every `F<n>` has an author reply before merge | ✓ | ✓ | – | per-PR `baseline` job (E2) reads the reviews and comments on the PR, fails while an `F<n>` has no reply. The daily check reports the last 14 days' merged PRs with unanswered findings | agent |
| G8 | Brand hygiene step (name-leak check) when `brand: none` | personal | – | – | step present in `ci` | agent |
| G9 | Repo settings: default branch `main`, squash only, delete branch on merge | ✓ | ✓ | – | API (reports *unknown*, never *pass*, when the reader can't see it) | **human** |
| G10 | Ruleset `protect-main` requiring `ci` + `baseline` (public repos; org repos after option C) | ✓ public | ✓ public | – | API | **human** (script Ahmed runs) |
| G11 | Secret `ATL_TOKEN` | ✓ | ✓ | – | inferred: the last `jira-close` run did not fail on auth (secret names aren't readable by the reader) | **human** (script Ahmed runs) |
| G12 | Agent Apps installed on the repo, as `github-identities.json` lists | ✓ | ✓ | – | registry ⇄ identities file | **human** (install clicks) + agent (config PR) |
| J1 | GitHub for Jira app covers the repo | ✓ | ✓ | ✓ via repos | JQL `project=<KEY> AND development[pullrequests].all > 0 AND updated >= -14d` ≥ 1 whenever a PR merged in 14 days | **human** (app repo selection) |
| J2 | Jira project exists, its workflow matches `jira_flow`, an unreleased Fix Version exists (full flow) | ✓ | ✓ | ✓ | atl read | **human** (Jira admin) |
| J3 | **Jira comment relay covers the project:** `jira-inbox.sh` (comments addressed to agents → their sessions) reads its project list from the registry. Today its JQL is hard-coded `project in (BASITA,DFP,ELB)`, so SLM comments never reach an agent | ✓ | ✓ | ✓ | the relay's JQL is built from the registry; the daily check confirms the key is in it | agent (registry) |
| C1 | Workers Builds connected: production branch `main`, non-production builds off | ✓ if deploy | – | – | the check run `deploy.check` succeeded on main's last commit | **human** (Cloudflare dashboard + Cloudflare App install) |
| C2 | Health endpoint answers after deploy | ✓ if deploy | – | – | `curl` from the Mac (overlay URL) | – |
| M1 | Mac automation covers it: pickup, jira-attention, janitor, playbook-sync | ✓ | ✓ | ✓ | automatic once the tools read the registry; the check confirms each tool sees the key | agent |
| M3 | **Character memory loads in every lead and pickup session.** Gap verified on 25/11: Malek's SLM session read `~/.claude/agents/malek.md` 5 times but never his own `~/.claude/agent-memory/malek/` or `vault/People/Malek.md`. `memory: user` in an agent file applies only when that agent *type* runs as a subagent; our leads are plain sessions told to read the file, so they get the folder's project memory (Talia's), not their own | ✓ | ✓ | ✓ | a test session briefed as a character shows its memory index in context; the daily check reads the hook's log for sessions that started with no character match | **Basel** (hook) |
| M2 | Cost category recorded for Sherif | ✓ | ✓ | ✓ | registry field present | agent |
| X1 | `main` is green (the last CI run on main succeeded) | ✓ | ✓ | – | API | – (a red main goes to the repo's lead) |

A gap is either fixed or **waived in the registry** with a reason, an end date and `approved_by: Ahmed`. An expired waiver counts as a gap again.

### 3. Bootstrap: `baseline bootstrap <KEY>` (playbook CLI, run by Basel)

1. Reads the registry entry, which a reviewed PR adds first.
2. Renders `templates/baseline/<profile>` and opens **one setup PR** in each repo as the `basita-basel` App (title `<KEY>-n: project baseline`). Re-running it on an existing repo opens a PR with only the missing files. That is also how SLM, basita-ops and others adopt.
3. Opens the **human checklist issue** for Ahmed: in BASITA, or in the project's own Jira for `personal`, so SLM never shows in BASITA. Each line has a direct link:
   - create the repo (if it doesn't exist) and the Jira project;
   - install the agent Apps and the GitHub for Jira App on the repo;
   - install the Cloudflare App and connect Workers Builds (the fields come from the registry);
   - run `baseline secrets <KEY>`: Ahmed runs it; it sets `ATL_TOKEN` from the Keychain (the general form of `slm-ops/enable-jira-close.sh`);
   - run `baseline protect <KEY>` (ruleset, repo settings): Ahmed runs it with his own gh, as a settings action, no commit.
4. Ends with `baseline check <KEY>`. The checklist issue closes only when the check is green (or waived), never on a workaround.

### 3a. Character context hook (M3)

- **Hook:** `UserPromptSubmit`, first prompt of a session only (a marker per session id). It reads the prompt, because a `SessionStart` hook doesn't see the brief.
- **Character detection:** the first match wins:
  1. a `Character: <name>` line, which becomes a standard line in every brief (Talia's briefs and `jira-pickup.sh`);
  2. `~/.claude/agents/<name>.md` named in the prompt;
  3. a title prefix «<Name>:».
- **What it injects** as additional context: the character's `~/.claude/agent-memory/<name>/MEMORY.md` index, plus the Canon and the last 10 Log lines of `vault/People/<Name>.md`, about 3–5k tokens, never whole memory files. The session reads individual memories as it does today.
- **No match:** nothing is injected, and one line goes to the hook log for the daily check.
- **Owner:** Basel builds it in basita-ops (`hook.sh` family) with a test. It applies to the Mac sessions; a cloud session gets the same text through the brief preamble.

### 4. Enforcement and drift detection

- **E1. Daily conformance on the Mac (detective; 0 Actions minutes).**
  - `baseline check --all` runs once a day at 07:30, scheduled by n8n (Execute Command), with launchd as the fallback if n8n is down.
  - It uses `agent-gh reader` and `atl yousef` reads, about 25 API calls per repo.
  - It writes `work/talia/baseline-report.md`: a matrix of project × item with pass / gap / unknown / waived.
  - It adds **one `attention-queue.md` line per new or changed gap** (fingerprinted, so a known gap isn't repeated daily) through `attention-add.sh`, plus waivers expiring within 7 days.
- **E2. A light per-PR job (preventive).**
  - `reusable-baseline.yml` runs on `ubuntu-slim` on `pull_request`, path-filtered to `.github/**`, `.ai-guard.toml`, `review-rules.json`, `package.json`, the coverage config and `pyproject.toml`.
  - It runs only the file-level checks (G1–G8) on the PR head, with no secrets and under a minute.
  - A PR that deletes or forks a baseline workflow turns red.
  - Cost: $0 on the public repos, and cents per month on private ones.
  - On the public repos the `protect-main` ruleset makes it required. On private org repos GitHub can't make it required (Free plan), so it is covered by the merge rule: only green PRs are merged (merge-ready rule), and `main-guard` reports a push that bypasses it.
- **E3. Baseline versions.** Adding an item bumps `baseline_version`. The daily check reports which repos are behind, and `baseline bootstrap <KEY>` brings them up with one PR.

### Later decision for Ahmed (not now): option C

GitHub Team for `basitasolutions` (≈ $4 per user per month) would let an **org ruleset** require `ci` + `baseline` and protect `main` on the private repos, so it would block instead of only detecting. My recommendation: **not now**. Revisit when either of these happens:
1. E1 finds a baseline drift that reached `main` on a private repo; or
2. a second client repo joins the org.

## Consequences

**Easier:**
- A new project means one registry PR, one bootstrap command and one checklist ticket.
- No tool can silently miss a project.
- Drift shows up within a day.
- SLM-type gaps become visible on day 1.

**Harder:**
- A small checker to maintain, about 300–400 lines of Python with tests.
- The atl refactor touches the close/done logic, so it gets Yousef's review at `xhigh`.

**Risks:**
- The reader identity can't see some settings. These report as *unknown* (never *pass*), and the report lists them so Ahmed can check by hand once.
- The registry in a public repo: only identifiers already public go there. Everything else goes in the private overlay.

**Why G7c/G7d are enforced, not assumed:** on 25/11, only 1 of the last 8 merged DFP PRs (#411) had a written reply from its author to the review. A rule in a file alone doesn't change that, so the `baseline` job checks for the replies.

**Watch:**
- Actions spend from E2: should be under $0.50/month.
- Unknown counts in the report.
- Time from "new repo" to "all green": target the same day.

**Revisit:** when GitHub Team is adopted (C), or when a non-Cloudflare deploy target appears (add a deploy kind).

## Implementation split (owner candidates, to confirm)

| # | Work | Owner | Model | Depends on |
|---|---|---|---|---|
| S1 | Registry + baseline schema (`config/projects.json`, `config/baseline.json`) + `src/baseline/check.py` (file checks + API probes) + tests | Malek | Sonnet 5.5 | – |
| S2 | `atl` reads the registry (all `*_PROJECTS` tuples, `PR_REPOS`); per-project behaviour moves to registry fields; the no-hard-coded-keys test | Malek | **Opus 5.5** (close/done logic is critical) | S1 |
| S3 | basita-ops scripts read the registry: `jira-agents.conf` values, `jira-attention.sh`, **`jira-inbox.sh` (comment relay)**, pickup dirs/ranks, janitor | Basel | Sonnet 5.5 | S1; after #120 / #121 merge |
| S4 | E1: n8n daily schedule + report + attention lines (fingerprinted) | Basel | Sonnet 5.5 | S1 |
| S5 | E2: `reusable-baseline.yml` + callers, including the G7d finding-reply check; `baseline protect` (rulesets on the public repos) and `baseline secrets` scripts for Ahmed | Basel | Sonnet 5.5 | S1 |
| S6 | `baseline bootstrap` + `templates/baseline/<profile>` + checklist-issue text | Malek (render) + Basel (checklist) | Sonnet 5.5 | S1, S5 |
| S8 | M3 character-context hook (`UserPromptSubmit`, first prompt) + the `Character:` line in Talia's brief template and in `jira-pickup.sh` | Basel | Sonnet 5.5 | – (independent) |
| S7 | Adopt: one bootstrap PR per existing repo to close today's gaps (first SLM, then basita-ops, playbook, DFP) | Basel | Sonnet 5.5 | S6 (or by hand from the gap list before it) |

S2 and S3 can run in parallel after S1. Yousef reviews S1, S2 and S6 at `xhigh`; S3–S5 at `high`.
