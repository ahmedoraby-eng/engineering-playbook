# ADR-024: Project baseline — one registry, one bootstrap, a daily conformance check

- **Status:** proposed (for Ahmed)
- **Date:** 2026-11-25
- **Deciders:** Ahmed (decision); Yousef (design); Basel and Malek (implementation)
- **Ticket:** BASITA-267

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
| J4 | **Confluence comment relay** (Ahmed, 25/11): doc review findings are posted as Confluence page comments that @mention the page owner; the owner replies there and fixes the page. Today nothing relays Confluence comments: `jira-inbox` reads Jira only, `pickup-context.sh` reads a page's footer comments only as context when a ticket starts, and `atl` has no comment command | ✓ | ✓ | ✓ | the relay reads new footer **and** inline comments that mention an agent in every registered space (space key in the registry), into the same inbox as Jira comments; the daily check confirms each space is read and each agent token carries the comment scopes | DevOps (relay, `atl <agent> confluence-comment` / `confluence-reply`) + Ahmed (token scopes) |
| C1 | Workers Builds connected: production branch `main`, non-production builds off | ✓ if deploy | – | – | the check run `deploy.check` succeeded on main's last commit | **human** (Cloudflare dashboard + Cloudflare App install) |
| C2 | Health endpoint answers after deploy | ✓ if deploy | – | – | `curl` from the Mac (overlay URL) | – |
| M1 | Mac automation covers it: pickup, jira-attention, janitor, playbook-sync | ✓ | ✓ | ✓ | automatic once the tools read the registry; the check confirms each tool sees the key | agent |
| M3 | **Character memory loads in every lead and pickup session.** Gap verified on 25/11: Malek's SLM session read `~/.claude/agents/malek.md` 5 times but never his own `~/.claude/agent-memory/malek/` or `vault/People/Malek.md`. `memory: user` in an agent file applies only when that agent *type* runs as a subagent; our leads are plain sessions told to read the file, so they get the folder's project memory (Talia's), not their own | ✓ | ✓ | ✓ | the daily check lists sessions that read an agent file but not its memory index | Talia (agent files) + Basel (detection) |
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

### 3a. Character memory at session start (M3)

Ahmed's proposal (25/11), accepted as the mechanism:
- **Where the rule goes:** the first section of every `~/.claude/agents/<name>.md` tells the session, before any work, to read `~/.claude/agent-memory/<name>/MEMORY.md` (the index, then only the memories it needs) and `vault/People/<Name>.md` (the Canon and the last 10 Log lines).
- **Why it reaches every session:** every lead brief and every pickup context already makes the session read its agent file, so this needs no new code. In the cloud the same paths map through basita-ops, which carries the vault.
- **Detection, not injection:** the daily check scans the day's lead and pickup transcripts. It lists any session that read an agent file but never its memory index, so a missed read shows up within a day.
- **Later, only if detection finds misses:** a `UserPromptSubmit` hook that injects the index.

### 3b. Confluence comment relay trigger (J4, decided 25/11)

**Decision: poll, every 15 minutes, with one CQL search.**
- **The search:** `type = comment AND space in (<registered space keys>) AND created >= "<cursor>"` returns footer and inline comments from every space in one call; replies are comments too.
- **Where it runs:** n8n on the Mac (the rule is that anything n8n can run is an n8n workflow), next to the Jira inbox.
- **What it keeps:** comments that @mention an agent's account, or that reply to a thread an agent started. It writes them into the same inbox as Jira comments, deduplicated by comment id.
- **The cursor:** the newest `created` seen, with a 2-minute overlap.
- **Cost:** about 96 API calls a day and no per-page loop.

**Rejected: the Confluence Automation event** («Comment added» trigger + «Send web request»):
- The Free plan allows about 10 rule runs a month per site ([pricing](https://www.atlassian.com/software/confluence/pricing), [announcement](https://community.atlassian.com/t5/Confluence-articles/Introducing-Confluence-Automation-in-Free-and-Standard-plans-Dec/ba-p/2890646)), so it stops after the first handful of comments.
- The Mac's n8n has no public URL, so the web request would need a tunnel exposed to the internet: more attack surface for a gain of minutes.
- **Revisit** if the Atlassian plan changes or a public, authenticated endpoint appears.

**Interim rule, until the relay exists, which then stays as a fast path:**
- An agent that posts a comment for a page owner also messages that owner's session directly, when one is open.
- Otherwise coordination routes it by hand.
- When the relay starts, it treats a comment that was already messaged as handled only if the owner has replied on the page. Duplicates are harmless because the relay dedupes by comment id.

### 3c. Delivery: from inbox row to the agent (J3/J4)

Every Pending row in the inbox (Jira or Confluence) is delivered with no hand step (full-automation goal, Ahmed 08/11):

1. **Session open:** if the agent's character hub or task session is open, the row goes to that session by session messaging.
2. **No session:** otherwise it starts one background session for that agent with `claude --bg`, the route `jira-pickup.sh` uses. The session takes all of that agent's Pending rows in one brief: a `Character: <name>` line, then each comment's text and link. It counts against the pickup caps.
3. **Reply:** the agent replies on the comment (Jira, or Confluence with `atl … confluence-reply`) and sets the row to Done.

Guards:
- **Known authors only:** comments by unknown authors are never delivered, the same known-account gate as pickup.
- **Comments are data:** comment text goes into the brief marked as untrusted data, never as instructions.
- **Escalation:** a row still Pending after 2 runs, a cap reached, or a failed start goes to `attention-queue.md`. Coordination handles only those.

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
| S8 | M3: the memory section at the top of all agent files (Talia, with Ahmed's approval on the Mac), plus the daily detection of sessions that skipped their memory (Basel) | Talia + Basel | Sonnet 5.5 | – (independent) |
| S9 | J4: Confluence comment relay (sibling of `jira-inbox.sh`, footer + inline comments, spaces from the registry), the `atl confluence-comment` / `confluence-reply` pair, and the comment scopes on every agent token (Ahmed's token step) | Basel (relay) + Malek (`atl`) | Sonnet 5.5 | S1 (registry) |
| S7 | Adopt: one bootstrap PR per existing repo to close today's gaps (first SLM, then basita-ops, playbook, DFP) | Basel | Sonnet 5.5 | S6 (or by hand from the gap list before it) |

S2 and S3 can run in parallel after S1. Yousef reviews S1, S2 and S6 at `xhigh`; S3–S5 at `high`.
