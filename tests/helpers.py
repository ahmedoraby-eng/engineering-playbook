"""Builders for synthetic diffs and PR bodies. Secrets are assembled at runtime so no
real-looking credential is ever committed to this repository."""

FAKE_AWS_KEY = "AKIA" + "Q" * 16
FAKE_GH_TOKEN = "ghp_" + "a1" * 18


def diff_for(files: dict[str, list[str]]) -> str:
    """Build a `git diff` that adds the given lines to each file."""
    parts = []
    for path, lines in files.items():
        parts += [
            f"diff --git a/{path} b/{path}",
            "index 0000000..1111111 100644",
            f"--- a/{path}",
            f"+++ b/{path}",
            f"@@ -1,0 +1,{len(lines)} @@",
            *[f"+{line}" for line in lines],
        ]
    return "\n".join(parts) + "\n"


def pr_body(no_ai: bool = False, assisted: bool = False) -> str:
    return (
        "## What and why\nFix the thing.\n\n## AI assistance\n\n"
        f"- [{'x' if no_ai else ' '}] No AI assistance\n"
        f"- [{'x' if assisted else ' '}] AI-assisted — I have read and understood every line\n"
    )
