# Contributing

Changes to the standards and to `ai-guard` go through pull requests like any other code.

1. Open an issue describing the problem a new or changed rule would prevent — ideally with a real incident or near miss.
2. Keep each PR to one standard or one rule. Fill in the PR template, including the AI-assistance box; the `ai-guard` check runs on every PR.
3. For `ai-guard` or `agent-evals` changes, add tests and keep coverage above 90%:

   ```bash
   ruff check . && ruff format --check . && pytest --cov=ai_guard --cov=agent_evals --cov-fail-under=90
   ```

4. A standard earns a *must* only when it is cheap to check and clearly prevents harm. Everything else is a *should*.

Changes to `.github/workflows/` or `.ai-guard.toml` that were AI-assisted need the `human-reviewed` label.
