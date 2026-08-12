"""Run the full deterministic Orchestrator fault-recovery acceptance suite."""

from __future__ import annotations

from collections import Counter
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent_runtime import AgentActionError
from app.evaluation.orchestrator_faults import (
    FIXED_ORCHESTRATOR_FAULT_CASES,
    run_orchestrator_fault_case,
)


def _case_passed(result) -> tuple[bool, str]:
    if result.case.recoverable:
        if result.exception is not None:
            return False, type(result.exception).__name__
        if not result.completed or not result.persisted:
            return False, f"status={result.state.status}, persisted={result.persisted}"
        if result.resume_state is None or result.resume_state.status != "completed":
            return False, "completed checkpoint is not resume-idempotent"
        return True, "recovered"

    if not isinstance(result.exception, AgentActionError):
        return False, "terminal case did not raise AgentActionError"
    if result.state.status != "failed" or not result.persisted:
        return False, f"status={result.state.status}, persisted={result.persisted}"
    return True, "failed safely"


def main() -> int:
    passed = 0
    print("case_id | result | status | steps | tools | llm | faults")
    print("-" * 96)
    for case in FIXED_ORCHESTRATOR_FAULT_CASES:
        result = run_orchestrator_fault_case(case)
        ok, detail = _case_passed(result)
        passed += int(ok)
        faults = Counter(event.mode.value for event in result.injector.events)
        fault_text = ",".join(f"{name}:{count}" for name, count in sorted(faults.items()))
        print(
            f"{case.case_id} | {'PASS' if ok else 'FAIL'} ({detail}) | "
            f"{result.state.status} | {result.state.current_step}/{result.state.max_steps} | "
            f"{result.state.tool_call_count} | {result.state.llm_call_count} | {fault_text}"
        )
    print()
    print(f"Orchestrator fault recovery: {passed}/{len(FIXED_ORCHESTRATOR_FAULT_CASES)} passed")
    return 0 if passed == len(FIXED_ORCHESTRATOR_FAULT_CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
