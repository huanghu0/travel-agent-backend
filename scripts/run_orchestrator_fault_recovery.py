"""运行完整 Orchestrator 故障恢复与安全终止验收套件。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation.fault_reporting import (
    build_fault_suite_report,
    write_fault_report_json,
    write_fault_report_junit,
)
from app.evaluation.orchestrator_faults import (
    FIXED_ORCHESTRATOR_FAULT_CASES,
    RECOVERABLE_ORCHESTRATOR_FAULT_CASES,
    TERMINAL_ORCHESTRATOR_FAULT_CASES,
    run_orchestrator_fault_case,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行真实确定性运行时的故障恢复与不可恢复终止策略基线。"
    )
    parser.add_argument(
        "--suite",
        choices=("all", "recovery", "terminal"),
        default="all",
        help="选择完整套件、仅可恢复场景或仅安全终止场景。",
    )
    parser.add_argument("--json-report", type=Path, help="可选 JSON 报告输出路径。")
    parser.add_argument("--junit-report", type=Path, help="可选 JUnit XML 输出路径。")
    return parser.parse_args()


def _selected_cases(suite: str):
    if suite == "recovery":
        return RECOVERABLE_ORCHESTRATOR_FAULT_CASES
    if suite == "terminal":
        return TERMINAL_ORCHESTRATOR_FAULT_CASES
    return FIXED_ORCHESTRATOR_FAULT_CASES


def main() -> int:
    args = _parse_args()
    results = [run_orchestrator_fault_case(case) for case in _selected_cases(args.suite)]
    report = build_fault_suite_report(results)

    print("case_id | category | result | status | steps | tools | llm | termination")
    print("-" * 120)
    for case in report.cases:
        detail = case.termination_code or case.completion_mode or "recovered"
        print(
            f"{case.case_id} | {case.category} | "
            f"{'PASS' if case.passed else 'FAIL'} | {case.status} | "
            f"{case.physical_steps}/{case.max_physical_steps} | "
            f"{case.tool_calls}/{case.max_tool_calls} | "
            f"{case.llm_calls}/{case.max_llm_calls} | {detail}"
        )
        if not case.passed:
            print(f"  failed checks: {', '.join(case.failed_check_codes)}")

    if args.json_report:
        output = write_fault_report_json(report, args.json_report)
        print(f"JSON report: {output}")
    if args.junit_report:
        output = write_fault_report_junit(report, args.junit_report)
        print(f"JUnit report: {output}")

    print()
    print(
        "Orchestrator fault safety: "
        f"{report.passed_case_count}/{report.total_case_count} passed "
        f"(recovery={report.recovery_pass_rate:.0%}, "
        f"terminal={report.terminal_pass_rate:.0%})"
    )
    return 0 if report.failed_case_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
