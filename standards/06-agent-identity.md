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
`scripts/atl <agent> jira-comment <KEY> "text"` and `scripts/atl <agent> confluence-append <page id> "text"` write through the Atlassian gateway with the agent's own **scoped API token**, read from the macOS Keychain at each call: `basita-atl-<agent>-jira` or `basita-atl-<agent>-confluence` (a scoped token covers one app), falling back to `basita-atl-<agent>`; account `basita`. The token is kept only in memory and never printed. Give each token the fewest scopes (Jira `write:comment:jira`; Confluence `read:page:confluence`, `write:page:confluence`) and the shortest expiry. Store it with a command that prompts, so it never lands in chat, files or shell history:

`scripts/atl <agent> jira-comment <KEY> "text" --mention <account id>` @mentions a person (repeatable), and every Jira comment ends with `— <Agent> (Basita AI)` so readers and the comment relay know it came from an agent. An agent's own transitions (Ahmed, 29-30/10/2026), each with a comment; they need the Jira scopes `read:jira-work` and `write:jira-work` besides `write:comment:jira`:
- `jira-start <KEY>`: To Do or Ready for Development to In Progress or In Development, with the `agent-picked` label (a second pickup is refused, and an issue in Refinement must be refined first);
- `jira-refine-start <KEY>` and `jira-refined <KEY>`: a refinement job keeps the issue in Refinement with the `agent-refining` label, then moves it to Ready for Development;
- `jira-review <KEY> --to <account id>`: work that needs a person's approval moves to the workflow's review status where there is one (DFP: Ready for Acceptance), always gets the `awaiting-approval` label, and is assigned to that person. Close-on-merge leaves such an issue open, so it never reaches Done or unblocks its dependants without the person;
- `jira-link <BLOCKER> <KEY>`: a Blocks link, so pickup waits for the blocker (needs the project's Link Issues permission).

Done comes from a merged PR with `Closes <KEY>`; every other transition stays with the human owner.
```bash
security add-generic-password -U -s basita-atl-salma-jira -a basita -w
```

## Enforcement
`reusable-agent-attribution.yml` checks every pull request: agent commits must carry a matching `Agent:` trailer and no Smart Commit command, unknown authors fail, AI-made commits authored as a human and missing issue keys are warnings. On a free private repository GitHub cannot make the check required, so the merge rule ("merge only on green") is the control.

This is attribution, not isolation: agents on one machine can reach each other's identities. Review and the check are what keep it honest.
