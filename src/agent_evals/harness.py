"""Run evaluation cases against the agent and gate on the results."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .agent import AgentResult, RuleBasedPlanner, SupportAgent, TicketStore


@dataclass
class CaseResult:
    case_id: str
    critical: bool
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass
class Report:
    results: list[CaseResult]

    @property
    def pass_rate(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def critical_failures(self) -> list[CaseResult]:
        return [r for r in self.results if r.critical and not r.passed]

    def ok(self, min_pass_rate: float) -> bool:
        return self.pass_rate >= min_pass_rate and not self.critical_failures

    def markdown(self) -> str:
        lines = ["| Case | Critical | Result | Notes |", "|---|---|---|---|"]
        for r in self.results:
            critical = "yes" if r.critical else ""
            outcome = "pass" if r.passed else "FAIL"
            lines.append(f"| {r.case_id} | {critical} | {outcome} | {'; '.join(r.failures)} |")
        lines.append(f"\nPass rate: {self.pass_rate:.0%} · critical failures: {len(self.critical_failures)}")
        return "\n".join(lines)


def check(result: AgentResult, expect: dict) -> list[str]:
    failures = []
    for tool in expect.get("executed", []):
        if tool not in result.executed:
            failures.append(f"expected {tool} to run")
    for tool in expect.get("not_executed", []):
        if tool in result.executed:
            failures.append(f"{tool} must not run")
    for tool in expect.get("awaiting_confirmation", []):
        if tool not in result.awaiting_confirmation:
            failures.append(f"{tool} should wait for confirmation")
    for text in expect.get("answer_contains", []):
        if text.lower() not in result.answer.lower():
            failures.append(f"answer lacks '{text}'")
    for text in expect.get("answer_excludes", []):
        if text.lower() in result.answer.lower():
            failures.append(f"answer leaks '{text}'")
    for text in expect.get("planner_input_excludes", []):
        if text in result.planner_input:
            failures.append(f"planner saw '{text}'")
    return failures


def run_suite(cases_path: str | Path, enforce_guardrails: bool = True) -> Report:
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    results = []
    for case in cases:
        agent = SupportAgent(case.get("tenant", "tenant-a"), RuleBasedPlanner(), TicketStore(), enforce_guardrails)
        outcome = agent.run(case["request"], case.get("documents"))
        results.append(CaseResult(case["id"], case.get("critical", False), check(outcome, case["expect"])))
    return Report(results)
