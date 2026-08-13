"""结构化 Orchestrator 故障恢复与安全终止报告。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

from pydantic import BaseModel, Field

from app.agent_runtime import (
    AgentActionError,
    AgentBudgetExceededError,
    AgentCheckpointError,
    AgentConvergenceError,
    AgentMaxStepsError,
)
from app.evaluation.orchestrator_faults import OrchestratorFaultResult


class FaultReportCheck(BaseModel):
    """单个故障场景的一条确定性验收断言。"""

    code: str
    passed: bool
    message: str
    expected: Any | None = None
    actual: Any | None = None


class FaultEventReport(BaseModel):
    """报告中的一次实际故障触发记录。"""

    target: str
    mode: str
    call_number: int = Field(ge=1)
    message: str


class OrchestratorFaultCaseReport(BaseModel):
    """单个完整 Orchestrator 场景的机器可读结果。"""

    case_id: str
    description: str
    category: Literal["recovery", "terminal_safety"]
    expected_outcome: Literal["completed", "failed"]
    actual_outcome: Literal[
        "recovered",
        "failed_safely",
        "unexpected_success",
        "unexpected_failure",
    ]
    passed: bool
    session_id: str
    status: str
    persisted: bool
    resume_idempotent: bool
    exception_type: str | None = None
    exception_message: str | None = None
    termination_code: str | None = None
    completion_mode: str | None = None
    quality_score: float | None = None
    issue_codes: list[str] = Field(default_factory=list)
    physical_steps: int = Field(ge=0)
    max_physical_steps: int = Field(ge=1)
    logical_actions: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    max_tool_calls: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    max_llm_calls: int = Field(ge=0)
    retries: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    budget_exceeded: bool
    fault_count: int = Field(ge=0)
    faults_by_mode: dict[str, int] = Field(default_factory=dict)
    fault_events: list[FaultEventReport] = Field(default_factory=list)
    provider_calls: dict[str, int] = Field(default_factory=dict)
    action_attempts: dict[str, int] = Field(default_factory=dict)
    checks: list[FaultReportCheck] = Field(default_factory=list)
    failed_check_codes: list[str] = Field(default_factory=list)


class OrchestratorFaultSuiteReport(BaseModel):
    """完整故障恢复与不可恢复终止策略基线。"""

    report_version: int = 1
    suite_name: str = "travel-agent-orchestrator-fault-safety-v1"
    generated_at: datetime
    total_case_count: int = Field(ge=0)
    recovery_case_count: int = Field(ge=0)
    terminal_case_count: int = Field(ge=0)
    passed_case_count: int = Field(ge=0)
    failed_case_count: int = Field(ge=0)
    recovery_pass_rate: float = Field(ge=0.0, le=1.0)
    terminal_pass_rate: float = Field(ge=0.0, le=1.0)
    overall_pass_rate: float = Field(ge=0.0, le=1.0)
    cases: list[OrchestratorFaultCaseReport] = Field(default_factory=list)


def _issue_codes(result: OrchestratorFaultResult) -> list[str]:
    codes: list[str] = []
    validation = result.state.last_validation_result
    if validation is not None:
        codes.extend(issue.code for issue in validation.issues)
    acceptance = result.state.acceptance_report
    if acceptance is not None:
        codes.extend(acceptance.unresolved_issue_codes)
    return list(dict.fromkeys(codes))


def _termination_code(
    result: OrchestratorFaultResult,
    issue_codes: list[str],
) -> str | None:
    """从真实异常与最终校验问题推导稳定的终止原因代码。"""

    exception = result.exception
    if exception is None:
        return None
    if isinstance(exception, AgentCheckpointError):
        return "checkpoint_retry_exhausted"
    if isinstance(exception, AgentMaxStepsError):
        return "max_steps_reached"
    if isinstance(exception, AgentBudgetExceededError):
        return "budget_exhausted"
    if isinstance(exception, AgentConvergenceError):
        return "convergence_stopped"
    if isinstance(exception, AgentActionError):
        last_result = result.state.last_action_result
        error_type = last_result.error_type.value if last_result and last_result.error_type else None
        if error_type == "authorization":
            return "authorization_failure"
        if (
            exception.action.value in {"generate_plan", "repair_plan"}
            and error_type == "invalid_output"
        ):
            return "llm_invalid_output_exhausted"
        if "route.unavailable" in issue_codes:
            return "route_unavailable_after_recovery"
        if "route.excessive_duration" in issue_codes:
            return "commute_replacement_exhausted"
        if "schedule.daily_overtime" in issue_codes:
            return "schedule_optimization_exhausted"
        if {"plan.no_attractions", "plan.insufficient_attractions"}.intersection(
            issue_codes
        ):
            return "attraction_candidates_exhausted"
        return f"action_failed:{exception.action.value}"
    return type(exception).__name__


def _check(
    checks: list[FaultReportCheck],
    code: str,
    passed: bool,
    message: str,
    *,
    expected: Any | None = None,
    actual: Any | None = None,
) -> None:
    checks.append(
        FaultReportCheck(
            code=code,
            passed=passed,
            message=message,
            expected=expected,
            actual=actual,
        )
    )


def build_fault_case_report(
    result: OrchestratorFaultResult,
) -> OrchestratorFaultCaseReport:
    """把运行结果转换成稳定、可用于 CI 的场景报告。"""

    case = result.case
    state = result.state
    checks: list[FaultReportCheck] = []
    issue_codes = _issue_codes(result)
    exception_type = type(result.exception).__name__ if result.exception else None
    termination_code = _termination_code(result, issue_codes)
    expected_status = "completed" if case.recoverable else "failed"
    expected_exception = None if case.recoverable else case.expected_exception_type

    _check(
        checks,
        "status.expected",
        state.status == expected_status,
        "最终状态必须符合场景预期。",
        expected=expected_status,
        actual=state.status,
    )
    _check(
        checks,
        "exception.expected",
        exception_type == expected_exception,
        "异常类型必须符合恢复或安全终止契约。",
        expected=expected_exception,
        actual=exception_type,
    )
    _check(
        checks,
        "checkpoint.persisted",
        result.persisted,
        "场景结束后必须至少保留一个可复盘的 SQLite 检查点。",
        expected=True,
        actual=result.persisted,
    )

    resume_idempotent = bool(
        result.resume_state is not None
        and result.resume_state.status == "completed"
        and result.resume_state.current_step == state.current_step
    )
    if case.recoverable:
        _check(
            checks,
            "resume.idempotent",
            resume_idempotent,
            "已完成检查点再次恢复时不得产生新的执行步骤。",
            expected=True,
            actual=resume_idempotent,
        )
    else:
        safe_terminal = state.status == "failed" and not isinstance(
            result.exception,
            (AgentMaxStepsError, AgentBudgetExceededError, AgentConvergenceError),
        )
        _check(
            checks,
            "terminal.explicit",
            safe_terminal,
            "不可恢复场景必须明确失败，不能退化成最大步骤、预算或收敛终止。",
            expected=True,
            actual=safe_terminal,
        )
        _check(
            checks,
            "termination.code",
            termination_code == case.termination_code,
            "终止原因代码必须由真实异常和最终问题推导得到。",
            expected=case.termination_code,
            actual=termination_code,
        )

    for expected_code in case.expected_issue_codes:
        _check(
            checks,
            f"issue.present:{expected_code}",
            expected_code in issue_codes,
            "最终结构化问题代码必须包含场景要求的问题。",
            expected=expected_code,
            actual=issue_codes,
        )

    triggered = {(event.target, event.mode) for event in result.injector.events}
    for rule in case.rules:
        _check(
            checks,
            f"fault.triggered:{rule.target}:{rule.mode.value}",
            (rule.target, rule.mode) in triggered,
            "配置的故障必须实际触发，避免测试只走正常路径。",
            expected=True,
            actual=(rule.target, rule.mode) in triggered,
        )

    budget_exceeded = bool(
        state.current_step > state.execution_budget.max_steps
        or state.tool_call_count > state.execution_budget.max_tool_calls
        or state.llm_call_count > state.execution_budget.max_llm_calls
        or isinstance(result.exception, AgentBudgetExceededError)
    )
    _check(
        checks,
        "budget.within_limits",
        not budget_exceeded,
        "故障恢复或安全终止不能突破步骤、工具和 LLM 生命周期预算。",
        expected=False,
        actual=budget_exceeded,
    )

    failed_check_codes = [check.code for check in checks if not check.passed]
    if case.recoverable:
        actual_outcome = "recovered" if not failed_check_codes else "unexpected_failure"
    elif result.exception is None:
        actual_outcome = "unexpected_success"
    else:
        actual_outcome = "failed_safely" if not failed_check_codes else "unexpected_failure"

    quality_score = None
    if state.acceptance_report is not None:
        quality_score = state.acceptance_report.quality_score

    fault_counter = Counter(event.mode.value for event in result.injector.events)
    return OrchestratorFaultCaseReport(
        case_id=case.case_id,
        description=case.description,
        category="recovery" if case.recoverable else "terminal_safety",
        expected_outcome="completed" if case.recoverable else "failed",
        actual_outcome=actual_outcome,
        passed=not failed_check_codes,
        session_id=state.session_id,
        status=state.status,
        persisted=result.persisted,
        resume_idempotent=resume_idempotent,
        exception_type=exception_type,
        exception_message=(str(result.exception)[:1000] if result.exception else None),
        termination_code=termination_code,
        completion_mode=state.completion_mode,
        quality_score=quality_score,
        issue_codes=issue_codes,
        physical_steps=state.current_step,
        max_physical_steps=state.execution_budget.max_steps,
        logical_actions=len(state.action_history),
        tool_calls=state.tool_call_count,
        max_tool_calls=state.execution_budget.max_tool_calls,
        llm_calls=state.llm_call_count,
        max_llm_calls=state.execution_budget.max_llm_calls,
        retries=state.total_retry_count,
        duration_ms=state.total_duration_ms,
        budget_exceeded=budget_exceeded,
        fault_count=len(result.injector.events),
        faults_by_mode=dict(sorted(fault_counter.items())),
        fault_events=[
            FaultEventReport(
                target=event.target,
                mode=event.mode.value,
                call_number=event.call_number,
                message=event.message,
            )
            for event in result.injector.events
        ],
        provider_calls=dict(sorted(result.provider_calls.items())),
        action_attempts=dict(sorted(state.attempts_by_action.items())),
        checks=checks,
        failed_check_codes=failed_check_codes,
    )


def build_fault_suite_report(
    results: list[OrchestratorFaultResult],
) -> OrchestratorFaultSuiteReport:
    """汇总恢复率、安全终止率、预算消耗和每个场景检查结果。"""

    cases = [build_fault_case_report(result) for result in results]
    recovery_cases = [case for case in cases if case.category == "recovery"]
    terminal_cases = [case for case in cases if case.category == "terminal_safety"]
    passed_count = sum(case.passed for case in cases)
    recovery_passed = sum(case.passed for case in recovery_cases)
    terminal_passed = sum(case.passed for case in terminal_cases)

    return OrchestratorFaultSuiteReport(
        generated_at=datetime.now(timezone.utc),
        total_case_count=len(cases),
        recovery_case_count=len(recovery_cases),
        terminal_case_count=len(terminal_cases),
        passed_case_count=passed_count,
        failed_case_count=len(cases) - passed_count,
        recovery_pass_rate=(
            recovery_passed / len(recovery_cases) if recovery_cases else 1.0
        ),
        terminal_pass_rate=(
            terminal_passed / len(terminal_cases) if terminal_cases else 1.0
        ),
        overall_pass_rate=passed_count / len(cases) if cases else 1.0,
        cases=cases,
    )


def write_fault_report_json(
    report: OrchestratorFaultSuiteReport,
    path: str | Path,
) -> Path:
    """写入适合归档和后续趋势分析的 UTF-8 JSON 报告。"""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return output


def write_fault_report_junit(
    report: OrchestratorFaultSuiteReport,
    path: str | Path,
) -> Path:
    """写入 CI 平台可直接识别的 JUnit XML 报告。"""

    suite = ET.Element(
        "testsuite",
        {
            "name": report.suite_name,
            "tests": str(report.total_case_count),
            "failures": str(report.failed_case_count),
            "errors": "0",
            "time": f"{sum(case.duration_ms for case in report.cases) / 1000:.3f}",
            "timestamp": report.generated_at.isoformat(),
        },
    )
    properties = ET.SubElement(suite, "properties")
    for name, value in (
        ("report_version", report.report_version),
        ("recovery_pass_rate", f"{report.recovery_pass_rate:.6f}"),
        ("terminal_pass_rate", f"{report.terminal_pass_rate:.6f}"),
        ("overall_pass_rate", f"{report.overall_pass_rate:.6f}"),
    ):
        ET.SubElement(properties, "property", {"name": name, "value": str(value)})

    for case in report.cases:
        test_case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": f"orchestrator_faults.{case.category}",
                "name": case.case_id,
                "time": f"{case.duration_ms / 1000:.3f}",
            },
        )
        if not case.passed:
            failed_messages = [
                f"{check.code}: {check.message} expected={check.expected!r} actual={check.actual!r}"
                for check in case.checks
                if not check.passed
            ]
            failure = ET.SubElement(
                test_case,
                "failure",
                {
                    "message": "; ".join(case.failed_check_codes),
                    "type": case.exception_type or "AcceptanceFailure",
                },
            )
            failure.text = "\n".join(failed_messages)
        system_out = ET.SubElement(test_case, "system-out")
        system_out.text = json.dumps(
            case.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(suite, space="  ")
    ET.ElementTree(suite).write(output, encoding="utf-8", xml_declaration=True)
    return output
