"""`python -m agent_evals evals/support_agent.json --min-pass-rate 0.9`"""

import argparse
import sys

from .harness import run_suite

parser = argparse.ArgumentParser(prog="agent-evals")
parser.add_argument("cases")
parser.add_argument("--min-pass-rate", type=float, default=1.0)
args = parser.parse_args()

report = run_suite(args.cases)
print(report.markdown())
sys.exit(0 if report.ok(args.min_pass_rate) else 1)
