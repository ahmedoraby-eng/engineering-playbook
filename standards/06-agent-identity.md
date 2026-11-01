# 06 — Agent identity and attribution

AI agents that write code are teammates with their own names, not ghosts inside a human's account. Every commit says who made it, so a reviewer, the audit trail and Jira's development panel can tell "Malek did this" from "Ahmed did this".

## Rules
1. **An agent commits under its own identity**: `<Name> (Basita AI) <name@agent-domain>`, as both author and committer. Use `scripts/agent-git <agent> commit ...`, which sets the identity for that one command and adds the trailers.
2. **Trailers on every agent commit:**
   ```
   Agent: Malek
   Requested-by: Ahmed Oraby <ahmed.oraby@icloud.com>
   Agent-Model: claude-opus-5-5        (optional)
   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
   ```
3. **Issue key** in the branch name or PR title (`DFP-123-short-name`), so Jira links the work.
4. **No Jira Smart Commit commands** (`#done`, `#comment`, `#time`) in agent commits. Jira changes go through the team's approval rules, not through commit messages.
5. **Pull requests opened under a human's account** that contain agent commits carry an `Agent: <Name>` line in the description.
6. Where an agent has its own GitHub App, it pushes and opens pull requests with that app's short-lived installation token, so the push and the pull request are attributed too.

## Without the wrapper (e.g. a cloud session)
```bash
git -c user.name="Malek (Basita AI)" -c user.email=malek@<agent-domain> commit \
  --trailer "Agent: Malek" --trailer "Requested-by: Ahmed Oraby <ahmed.oraby@icloud.com>" -m "DFP-123: ..."
```

## Jira and Confluence as the agent (trial)
The cloud id defaults to Basita's site (`basitasolutions.atlassian.net`), so `atl` needs no environment setup in an autonomous session; `ATL_CLOUD_ID` or `--cloud-id` override it.

The read commands print JSON and change nothing: `scripts/atl <agent> jira-get <KEY>` (an issue with its comments, sub-tasks and linked issues) and `jira-jql "<JQL>"`. Each result says `read_by`, the agent whose token answered. A pickup worker gets its context as files from `jira-pickup.sh` and uses these only for what that snapshot did not anticipate, because the Atlassian MCP tools are locked on its routine. `scripts/atl <agent> confluence-get <page id or URL>` and `confluence-search "<words or CQL>"` read Confluence, and `confluence-create <space key> "text" --title "Title"` and `confluence-edit <page id or URL> "text"` write it, each as the agent. Nothing in `atl` deletes.

A Confluence token (`basita-atl-<agent>-confluence`) is a scoped token for the Confluence app with exactly these scopes: `read:page:confluence`, `read:space:confluence`, `read:comment:confluence`, `read:attachment:confluence`, `read:hierarchical-content:confluence`, `write:page:confluence`, `search:confluence` and `read:content-details:confluence` (the search endpoint lists the last two as alternatives). `delete:page:confluence` is left out on purpose.

`scripts/atl <agent> jira-comment <KEY> "text"` and `scripts/atl <agent> confluence-append <page id> "text"` write through the Atlassian gateway with the agent's own **scoped API token**, read from the macOS Keychain at each call: `basita-atl-<agent>-jira` or `basita-atl-<agent>-confluence` (a scoped token covers one app), falling back to `basita-atl-<agent>`; account `basita`. The token is kept only in memory and never printed. Give each token the scopes below and no others, with the longest expiry Atlassian allows (Ahmed, 31/10/2026). A Jira token (`basita-atl-<agent>-jira`) has `read:jira-work`, `write:jira-work` and `write:comment:jira`; a Confluence token has the eight listed above. Store it from the clipboard, not at the prompt: `security add-generic-password -w` at its interactive prompt cuts a token at 128 characters, and Atlassian scoped tokens are longer, so a token stored that way fails with 401. Copy the token, then run the command below. It stores only a value that starts with `ATATT` and has no spaces, so a wrong paste is refused rather than stored, and it keeps the token out of chat, files and shell history. Clear the clipboard afterwards (`pbcopy </dev/null`). Then run `scripts/atl-check <agent> jira` or `scripts/atl-check <agent> confluence`, which checks the stored value's shape without printing it and reads as the agent.

`scripts/atl <agent> jira-comment <KEY> "text" --mention <account id>` @mentions a person (repeatable), and every Jira comment ends with `— <Agent> (Basita AI)` so readers and the comment relay know it came from an agent. `scripts/atl adam jira-return <KEY> "reason"` (Adam only, only from Refinement) moves an issue to `Ready for Testing` and assigns it to Salma with the reason as a comment (operating model §4.3 item 11). `scripts/atl salma jira-dismiss <KEY> --reason "Not a Bug|Already Fixed|Duplicate|Business Requirement"` (Salma only, only from `In Testing`) moves it to `Closed` (item 12). `jira-start` sends Salma from `Ready for Testing` to `In Testing` and refuses every other agent there; in DFP Salma pulls from nothing else. URLs and `[label](url)` in comments are posted as clickable links (§4.4 item 5). `scripts/atl <agent> jira-ready <KEY>` moves an issue the agent refined from Refinement to Ready (`Ready for Development` in DFP; standing approval, operating model §4.3 item 9; never an Epic). `scripts/atl <agent> jira-start <KEY>` is an agent's other transition: To Do to In Progress, with the `agent-picked` label (a second pickup is refused) and a comment; it needs `read:jira-work` and `write:jira-work`. Done comes from a merged PR with `Closes <KEY>`; every other transition stays with the human owner.
```bash
t="$(pbpaste)"; if [[ "$t" == ATATT* && "$t" != *[[:space:]]* ]]; then security add-generic-password -U -s basita-atl-salma-jira -a basita -w "$t"; else echo "Not a token: nothing stored"; fi; unset t
```

## Agents with no seat on a product
An agent with no seat on a product works through another agent's user there, with `--via`. Maya has no Jira seat: `scripts/atl maya --via sherif jira-comment DFP-1 "text"` uses Sherif's Jira token and signs `— Maya (Basita AI, via Sherif)`, so Jira shows that account and the signature says who did the work. She has her own Confluence user, so `scripts/atl maya confluence-get <page>` takes no `--via`. An agent with no Atlassian user and no `--via` fails on the missing Keychain item. In git they are ordinary agents: `scripts/agent-git maya commit` commits as `Maya (Basita AI) <maya@<agent-domain>>`.

## Enforcement
`reusable-agent-attribution.yml` checks every pull request: agent commits must carry a matching `Agent:` trailer and no Smart Commit command, unknown authors fail, AI-made commits authored as a human and missing issue keys are warnings. On a free private repository GitHub cannot make the check required, so the merge rule ("merge only on green") is the control.

This is attribution, not isolation: agents on one machine can reach each other's identities. Review and the check are what keep it honest.
