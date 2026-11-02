"""A picture of DFP's flow, drawn from the table in client.py, and a read-only check of it against Jira.

The picture is generated, never drawn by hand, so it can't differ from the table the code runs on:

    python -m atl_agent.flowchart            # print the picture (Markdown with a Mermaid diagram)
    python -m atl_agent.flowchart --write    # rewrite standards/dfp-flow.md; a test fails when it is out of date
    python -m atl_agent.flowchart check      # ask Jira's GET /transitions what each work type may do, per status

`check` changes nothing: it makes GET calls only, as an agent, with that agent's Keychain token, and moves no issue.
For each status the steps leave and each work type, it takes one live issue and reads its transitions:
- all of them global: "allows any move". A finding, not a failure; the table's moves are still checked to exist.
- some not global: Jira has been narrowed, so its moves must be exactly the table's. Any difference exits 1.
- no live issue in that status and type: "not checked", never a pass.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .client import (
    BUG_DIRECT_STATUS,
    FLOW,
    JIRA_ACCOUNTS,
    LAST_ENGINEER,
    AtlClient,
    AtlError,
    default_cloud_id,
    keychain_token,
)
from .close import GUARDED

PICTURE = Path(__file__).resolve().parents[2] / "standards" / "dfp-flow.md"

# The moves other commands make. They are not rows of FLOW, so the picture draws them dashed, with the command that
# makes each one. Their statuses are written inside those commands, so tests/test_atl_flowchart.py runs the real
# command for every row and asserts its from and to. A row here with no such test fails. The list shrinks as these
# steps move into FLOW. The merge's statuses come from GUARDED and the direct Bug's from BUG_DIRECT_STATUS.
OTHER_STEPS = {
    "ready": {
        "command": "jira-ready",
        "agents": ("adam",),
        "from": "Refinement",
        "to": "Ready for Development",
        "assign": "an engineer (--assignee)",
    },
    "return": {
        "command": "jira-return",
        "agents": ("adam",),
        "from": "Refinement",
        "to": "Ready for Testing",
        "assign": JIRA_ACCOUNTS["salma"],
    },
    "bug to engineer": {
        "command": "jira-create",
        "agents": ("salma",),
        "from": "Refinement",
        "to": BUG_DIRECT_STATUS,
        "assign": "Malek or Haya (--assignee)",
    },
    "start (engineer)": {
        "command": "jira-start",
        "agents": ("malek", "haya"),
        "from": "Ready for Development",
        "to": "In Development",
        "assign": "same",
    },
    "start (testing)": {
        "command": "jira-start",
        "agents": ("salma",),
        "from": "Ready for Testing",
        "to": "In Testing",
        "assign": "same",
    },
    "dismiss": {
        "command": "jira-dismiss",
        "agents": ("salma",),
        "from": "In Testing",
        "to": "Closed",
        "assign": "no one",
    },
    "merged": {
        "command": "the merge (close.py)",
        "agents": ("merge job (Sherif's account)",),
        "from": GUARDED["DFP"][0],
        "to": GUARDED["DFP"][1],
        "assign": "no one",
    },
}
# Decided in BASITA-41 and not built: the picture draws none of these, so no one takes them for working steps.
DECIDED_NOT_BUILT = ("the status In Acceptance", "refuse", "blocked", "restart", "reset")
# What Jira allowed on 02/11/2026, read with `check`. Change this text when Jira's DFP workflow is narrowed.
WHAT_JIRA_ENFORCES = (
    "Jira does not enforce this flow yet: DFP's workflow lets any status reach any status, so only `atl` keeps to "
    "these arrows (read with `python -m atl_agent.flowchart check`, 02/11/2026). Once Jira is narrowed, Jira's own "
    "workflow diagram is the one that is enforced."
)
# The work types DFP's workflows are set for (a team-managed workflow is per work type). An Epic is outside the flow.
WORK_TYPES = ("Story", "Task", "Bug", "Subtask")

_NAMES = {account: name.capitalize() for name, account in JIRA_ACCOUNTS.items()}


def _who(agents: tuple[str, ...]) -> str:
    return " or ".join(a.capitalize() if a.isalpha() else a for a in agents)


def _next(assign: str) -> str:
    return "the last engineer" if assign == LAST_ENGINEER else _NAMES.get(assign, assign)


def _steps() -> list[dict]:
    """FLOW's rows first, then the other commands'. Each has its action, command, who, from, to and next owner."""
    steps = [
        {"action": a, "command": "jira-flow", "who": _who(s["agents"]), "from": s["from"], "to": s["to"],
         "next": _next(s["assign"]), "table": True}
        for a, s in FLOW.items()
    ]  # fmt: skip
    steps += [
        {"action": a, "command": s["command"], "who": _who(s["agents"]), "from": s["from"], "to": s["to"],
         "next": _next(s["assign"]), "table": False}
        for a, s in OTHER_STEPS.items()
    ]  # fmt: skip
    return steps


def _node(status: str) -> str:
    return re.sub(r"\W+", "_", status.lower()).strip("_")


def _walk(steps: list[dict]) -> list[str]:
    """The statuses in the order a walk reaches them, from the one no step leads to, so the picture reads top down."""
    targets = {s["to"] for s in steps}
    order = [next((s["from"] for s in steps if s["from"] not in targets), steps[0]["from"])]
    for status in order:  # the list grows while we walk it
        for s in steps:
            if s["from"] == status and s["to"] not in order:
                order.append(s["to"])
    return order + [st for s in steps for st in (s["from"], s["to"]) if st not in order]


def render() -> str:
    steps = _steps()
    statuses = _walk(steps)
    lines = [
        "# DFP flow (Dairy Farm Platform only)",
        "",
        "<!-- Generated by `python -m atl_agent.flowchart --write` from FLOW and OTHER_STEPS. Do not edit by hand: "
        "a test fails when this file and the tables differ. -->",
        "",
        "This shows what `atl` allows. " + WHAT_JIRA_ENFORCES,
        "",
        "Solid arrows are the steps of `jira-flow`, the table in `src/atl_agent/client.py`. Dashed arrows are made "
        "by other commands. Each arrow says the action, who may make it, and who owns the issue next.",
        "",
        "```mermaid",
        "flowchart TD",
    ]
    lines += [f'    {_node(st)}["{st}"]' for st in statuses]
    solid = dashed = 0
    for s in steps:
        label = f"{s['action']} · {s['who']}<br/>next: {s['next']}"
        arrow = "-->" if s["table"] else "-.->"
        lines.append(f'    {_node(s["from"])} {arrow}|"{label}"| {_node(s["to"])}')
        if s["table"]:
            solid += 1
        else:
            dashed += 1
    lines += ["```", "", "| Action | Command | Who | From | To | Next owner |", "|---|---|---|---|---|---|"]
    lines += [
        f"| {s['action']} | `{s['command']}` | {s['who']} | {s['from']} | {s['to']} | {s['next']} |" for s in steps
    ]
    lines += [
        "",
        f"{solid} steps are rows of the `jira-flow` table; {dashed} are made by other commands.",
        "",
        f"Decided, not built (BASITA-41), so not drawn: {', '.join(DECIDED_NOT_BUILT)}.",
        "",
    ]
    return "\n".join(lines)


def check(client: AtlClient, project: str = "DFP") -> tuple[list[str], bool]:
    """Compare the steps with Jira's GET /transitions, on one live issue per status and work type. Returns the report
    lines and whether nothing differs. Makes GET calls only and moves no issue."""
    steps = _steps()
    report: list[str] = []
    ok = True
    tally = {"not checked": 0, "any move": 0, "exact": 0, "differs": 0}
    for status in dict.fromkeys(s["from"] for s in steps):
        wanted = {s["to"].lower(): s["to"] for s in steps if s["from"] == status}
        for work_type in WORK_TYPES:
            jql = f'project = {project} AND status = "{status}" AND issuetype = "{work_type}" ORDER BY updated DESC'
            found = client.jira_jql(jql, limit=1)
            if not found["issues"]:
                tally["not checked"] += 1
                report.append(f"not checked: {status} / {work_type} (no {project} issue of that type in it)")
                continue
            key = found["issues"][0]["key"]
            moves = [m for m in client.jira_transitions(key) if m["to"].lower() != status.lower()]
            offered = {m["to"].lower(): m["to"] for m in moves}
            missing = sorted(wanted[t] for t in wanted if t not in offered)
            extra = sorted(offered[t] for t in offered if t not in wanted)
            where = f"{status} / {work_type} on {key}"
            if all(m["global"] for m in moves):
                tally["any move"] += 1
                note = f"MISSING {', '.join(missing)}" if missing else f"the table's {len(wanted)} moves exist"
                report.append(f"allows any move: {where}: {len(moves)} moves, all global; {note}")
                if missing:
                    tally["differs"] += 1
                    ok = False
            elif missing or extra:
                tally["differs"] += 1
                ok = False
                report.append(
                    f"DIFFERS: {where}: missing {', '.join(missing) or 'none'}; not in the table "
                    f"{', '.join(extra) or 'none'}"
                )
            else:
                tally["exact"] += 1
                report.append(f"ok: {where}: exactly the table's {len(wanted)} moves")
    report.append(
        "Statuses and work types: "
        + ", ".join(f"{n} {name}" for name, n in tally.items())
        + '. "Not checked" is not a pass. A move missing here may be this token\'s rights, not the workflow: '
        "GET /transitions answers for the calling user."
    )
    return report, ok


def main(argv: list[str] | None = None, token_source=keychain_token, transport=None) -> int:
    parser = argparse.ArgumentParser(prog="flowchart", description=__doc__.split("\n")[0])
    parser.add_argument("command", nargs="?", choices=["check"], help="check: compare with Jira (read-only)")
    parser.add_argument("--write", action="store_true", help=f"write the picture to {PICTURE.name}")
    parser.add_argument("--agent", default="malek", help="check: whose token reads Jira (default malek)")
    parser.add_argument("--cloud-id", default=default_cloud_id())
    args = parser.parse_args(argv)
    if args.command == "check":
        try:
            kwargs = {"transport": transport} if transport else {}
            client = AtlClient(args.agent, args.cloud_id, token_source(args.agent, "jira"), **kwargs)
            report, ok = check(client)
        except AtlError as error:
            print(f"atl: {error}", file=sys.stderr)
            return 1
        print("\n".join(report))
        print(
            "result:",
            "nothing that was checked differs from the table" if ok else "Jira differs from the table",
        )
        return 0 if ok else 1
    if args.write:
        PICTURE.write_text(render())
        print(f"wrote {PICTURE}")
    else:
        print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
