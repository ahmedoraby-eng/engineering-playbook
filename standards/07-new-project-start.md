# New project start checklist

**Standard 07 · project baseline (ADR-024).** Owner: architecture. Builders: DevOps and engineering. Version 1, 25/11/2026.

Every new project (a new repo, a new Jira project, or both) goes through this list **before its first feature PR**. An existing repo that joins goes through it too.

- **Where the items come from:** each one was found missing, by hand, in the Smart Library (SLM) project on 24–25/11/2026. SLM shipped 21 PRs before anyone noticed, because the shared setup is opt-in per repo.
- **Running the check:** `scripts/new-project-check <owner/repo> <JIRA_KEY>` reports which items a project is missing. The daily conformance check (ADR-024 E1) runs the same checks on every registered project.
- **Human steps:** a step that needs a human click (an app install, OAuth, a secret, a Jira admin setting) becomes **one checklist issue for Ahmed**. It is never a silent gap.

**Legend**
- **Enforced by:**
  - **check**: `new-project-check` and the daily run;
  - **CI**: a required workflow;
  - **hook**: a Mac hook;
  - **manual**: a human confirms, and the check records the confirmation.
- **Owner:** who sets the item up. Ahmed is listed only where a click is his.

## A. Registry and Jira

| # | Item | Why (what SLM showed) | Enforced by | Owner |
|---|---|---|---|---|
| A1 | The project is in the **registry** (`config/projects.json`): key, repos, profile (`product` / `personal` / `ops` / `jira-only`), brand, deploy, cost category, pickup on/off | Five tools kept their own hard-coded project lists, and SLM was in none: `atl` close (playbook #76), `jira-attention`, pickup, janitor (basita-ops #120/#121) and the comment relay | check: every tool's list is built from the registry; a test fails on a hard-coded key | DevOps |
| A2 | **Jira project** exists, its statuses and transitions are mapped (simple: To Do → In Progress → Done; full: Refinement → … → Released), and an unreleased **Fix Version** exists | `jira-close` had to learn SLM's status mapping (`Closes` → Released from any status) after the fact. Pickup needs a Fix Version to plan anything | check (`atl` read of the workflow and versions) | Ahmed (Jira admin) + product owner |
| A3 | **Stage flow:** tickets go through the pickup stages, so the product owner, QA and coordination each comment | SLM tickets carry 1–5 comments from one or two roles, against 9–19 from several roles on DFP. Refinement, testing and acceptance happened outside the ticket | check: per-ticket count of comment roles at Released; pickup on in the registry | coordination |
| A4 | **Jira comment relay** (`jira-inbox.sh` + n8n WF-02) includes the key | The relay JQL was `project in (BASITA,DFP,ELB)`, so SLM comments never reached an agent (fix in basita-ops #122) | check: the relay's JQL comes from the registry | DevOps |
| A5 | **jira-attention** (merged but not Done) and the **flow audit** include the key | SLM issues stayed open after their PRs merged (basita-ops #120) | check | DevOps |
| A7 | **Confluence comment relay** covers the project's space: page comments (footer and inline) that @mention an agent reach that agent, who replies on the page and fixes it; agents can comment and reply with `atl` | Ahmed wants doc review findings as page comments to the page owner; on 25/11 nothing relayed Confluence comments, so SLM review findings were routed by hand | check: the relay's space list comes from the registry; token scopes checked | DevOps |
| A6 | **Cost category** for the AI-spend log | SLM costs had to be split out by hand as «Personal – SLM» | check: registry field | coordination |

## B. GitHub repository

| # | Item | Why (what SLM showed) | Enforced by | Owner |
|---|---|---|---|---|
| B1 | **Agent instruction files:** `AGENTS.md` + `CLAUDE.md` from the project template. They carry identity and `agent-git`, `Refs`/`Closes <KEY>`, the review-conversation rule, reading tickets from the snapshot, no merges by agents, and the forbidden names when `brand: none` | The SLM repo had no instruction file, so agents working there got no rules (Ahmed, 25/11: «ملفات التعليمات للايجنتس على كل ريبو، ودي المفروض تبقى في التمبلت») | check + bootstrap renders them | engineering |
| B2 | **PR template** with the `Refs`/`Closes <KEY>` line and the AI-assistance checkbox | SLM had none until SLM #22. ai-guard needs the checkbox | check + CI (ai-guard) | engineering |
| B3 | **CI gates** from the playbook's reusable workflows: lint + format, typecheck, tests with **90 % coverage**, dependency **audit**, build | SLM CI had no coverage gate, lint or audit. The backfill raised web coverage from 79.6 % to 99.6 % and found 2 real bugs (SLM #26) | CI + check (caller `uses:` the playbook, not a local copy) | engineering |
| B4 | **ai-guard** + `.ai-guard.toml` (AI box, tests with source, protected paths need `reviewed-by-ahmed`) | Missing on SLM for 21 PRs | CI + check | engineering |
| B5 | **main-guard**, **architecture-review** (+ rerun), **agent-attribution** callers | Missing on SLM (playbook and basita-ops lack attribution too) | CI + check | engineering |
| B6 | **Hygiene grep** for names that must not appear (for `brand: none` projects) and a **secret scan** | SLM is personal: no employer or client names in code, UI, URL, OAuth app or docs | CI (fails the build) | engineering |
| B7 | **Review conversation:** findings numbered F1, F2…; the author replies to each in the PR (fixed + commit, or why not); a re-review marks each one resolved or open | Ahmed, 25/11. Only 1 of the last 8 merged DFP PRs had a written reply from its author | CI: the `baseline` job fails while an `F<n>` has no reply; daily report | engineering + architecture |
| B8 | **`protect-main` ruleset** requiring the CI checks (free on public repos; private org repos need GitHub Team) | SLM had no ruleset, so a red `main` (test failure on the #24 merge) went unnoticed | check (API) | Ahmed (script he runs) |
| B9 | **Repo settings:** default branch `main`, squash merge, delete branch on merge | Unknown on every repo: the read identity can't see them | manual + check reports «unknown» | Ahmed |
| B10 | **Decision register from day one:** `docs/adr/NNNN-*.md` + `docs/decisions.md`, mirrored as a Confluence register page | SLM had a 16-row table and no history. 183 decisions had to be recovered from files, tickets and conversations (SLM #27) | check: the files exist and the README links the register | architecture |

## C. Jira ↔ GitHub, deploy and close

| # | Item | Why (what SLM showed) | Enforced by | Owner |
|---|---|---|---|---|
| C1 | **GitHub for Jira** app installed on the **right account** (org or personal) and the repo selected | SLM's Development panel was empty: the app didn't include the repo on the personal account | check: JQL `development[pullrequests].all > 0` on recently merged keys | Ahmed (app click) |
| C2 | **`jira-close`** workflow with `deploy-check`: `Closes <KEY>` moves the issue to Released only after the deploy check succeeds; `Refs` moves nothing | Added late (SLM #22). Before it, issues were closed by hand or left open | CI + check | engineering; Ahmed sets `ATL_TOKEN` |
| C3 | **Auto-deploy** (Cloudflare Workers Builds or the target's equivalent): production branch `main`, non-production builds off, the check name recorded in the registry | Every SLM deploy was manual until SLM #24. Then the Workers Builds check didn't appear on `main` until the dashboard connection was made | check: the deploy check succeeded on main's last commit | Ahmed (dashboard + Cloudflare app) + DevOps |
| C4 | **Migrations gate:** a PR that changes `migrations/` needs the label `migrations-applied`; migrations run by hand before merge | Auto-deploy doesn't run migrations; a schema change must not land unattended (SLM #25) | CI | DevOps |
| C5 | **Health endpoint** checked after each deploy | The first auto-deploy needed a manual `/api/health?deep=1` check | check (curl from the Mac) | DevOps |

## D. Identities, credentials and brand

| # | Item | Why (what SLM showed) | Enforced by | Owner |
|---|---|---|---|---|
| D1 | **Agent GitHub Apps** installed on the repo, as `github-identities.json` lists them | SLM needed playbook #75 to add the repo to the App installations | check: registry ⇄ identities file | Ahmed (install clicks) + DevOps |
| D2 | **Commit identity per owner:** the commit email suits the repo (personal vs company; public repos show it). Bot avatars and names are neutral where the brand rule applies | Commits to a personal public repo show their author email and bot names | check: the last 20 commits' author emails match the registry's allowed list | DevOps |
| D3 | **Push credentials:** pushes use the `gh` credential helper or the agent App, never the read-only reader's Keychain item | Found during SLM setup: a push must go through the `gh` credential helper or the agent App, not the read-only `basita-reader` Keychain item, which can't write | manual (documented in `AGENTS.md`) | DevOps |
| D4 | **Secrets** in place by name (`ATL_TOKEN`, deploy-side secrets), set by a script Ahmed runs, never typed into chat or committed | `ATL_TOKEN` was set late, by `enable-jira-close.sh` | check: inferred from the last `jira-close` run | Ahmed |

## E. The Mac and the sessions

| # | Item | Why (what SLM showed) | Enforced by | Owner |
|---|---|---|---|---|
| E1 | **Janitor** covers the project's worktrees | SLM worktrees piled up outside the janitor (basita-ops #121) | check: registry ⇄ `JANITOR_REPOS` | DevOps |
| E2 | **Character memory loads** in every lead and pickup session: a `Character: <name>` line in the brief, and a hook that injects the character's memory index and vault Canon | A lead session on SLM read its agent file 5 times but never its own memory | hook + daily log check | DevOps |
| E3 | **Session renewal at about 300k tokens**, with a handover file | Large SLM sessions (the coordination session reached tens of MB of transcript) cost more per turn and lose earlier context | manual (coordination check-in) | coordination |
| E4 | **Confluence space** for the project, with a Home page that links every section, including the ADR register | The SLM Home page missed pages 6 and 7 | manual | architecture + coordination |

## How to start a project

1. Add the registry entry (one PR to the playbook).
2. Run `baseline bootstrap <KEY>`. It opens the setup PR with B1–B7 and B10, and the checklist issue for Ahmed with A2, B8, B9, C1, C3, D1 and D4, each with a direct link.
3. Ahmed does the clicks on the checklist issue.
4. Run `scripts/new-project-check <owner/repo> <KEY>` until it shows no gap, or each remaining gap is waived in the registry (with a reason, an end date and Ahmed's approval).
5. Only then the first feature PR.

Until `baseline bootstrap` exists (ADR-024 S6), do steps 2 and 3 by hand from this list.
