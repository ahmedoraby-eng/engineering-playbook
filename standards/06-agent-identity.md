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

## Enforcement
`reusable-agent-attribution.yml` checks every pull request: agent commits must carry a matching `Agent:` trailer and no Smart Commit command, unknown authors fail, AI-made commits authored as a human and missing issue keys are warnings. On a free private repository GitHub cannot make the check required, so the merge rule ("merge only on green") is the control.

This is attribution, not isolation: agents on one machine can reach each other's identities. Review and the check are what keep it honest.
