"""固定城市、天数、交通方式和偏好的端到端验收基线。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from app.agent_runtime.state import AgentState
from app.evaluation.models import (
    AcceptanceCaseResult,
    AcceptanceCheckResult,
    AcceptanceScenario,
    FixedAcceptanceBaselineReport,
)
from app.schemas.trip_schema import TripRequest


def _infer_completion_mode(state: AgentState) -> str | None:
    """兼容旧检查点：历史 completed 会话没有模式时按完整完成处理。"""

    if state.status != "completed":
        return None
    return "partial" if state.completion_mode == "partial" else "full"


def _state_quality_level(state: AgentState) -> str | None:
    """提取最终质量等级；没有部分接受报告时返回空值。"""

    report = state.acceptance_report
    if report is None:
        return None
    return report.quality_level.value


def _scenario_request(
    *, city: str, start: date, days: int, transportation: str, preferences: list[str]
) -> TripRequest:
    end = start + timedelta(days=days - 1)
    return TripRequest(
        city=city,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        travel_days=days,
        transportation=transportation,
        accommodation="经济型酒店",
        preferences=preferences,
        free_text_input="",
    )


def build_fixed_acceptance_scenarios() -> list[AcceptanceScenario]:
    """生成 15 个不可随运行时间漂移的固定验收输入。"""

    city_preferences = {
        "杭州": ["休闲", "自然风光"],
        "北京": ["历史文化", "城市观光"],
        "上海": ["亲子", "城市观光"],
        "成都": ["美食", "休闲"],
        "西安": ["历史文化", "美食"],
    }
    transports = {
        1: ("步行", "walking"),
        3: ("公共交通", "transit"),
        5: ("驾车", "driving"),
    }
    city_slugs = {
        "杭州": "hangzhou",
        "北京": "beijing",
        "上海": "shanghai",
        "成都": "chengdu",
        "西安": "xian",
    }
    scenarios: list[AcceptanceScenario] = []
    # 固定日期从 2026-10-12 开始，全部晚于本阶段开发日期 2026-08-12。
    base_date = date(2026, 10, 12)
    for city_index, (city, preferences) in enumerate(city_preferences.items()):
        for duration_index, days in enumerate((1, 3, 5)):
            start = base_date + timedelta(days=city_index * 28 + duration_index * 7)
            transportation, transport_slug = transports[days]
            scenarios.append(
                AcceptanceScenario(
                    case_id=f"{city_slugs[city]}-{days}d-{transport_slug}",
                    description=(
                        f"{city}{days}日{transportation}固定验收，覆盖路线、时间轴、"
                        "通勤、约束和最低景点保障"
                    ),
                    request=_scenario_request(
                        city=city,
                        start=start,
                        days=days,
                        transportation=transportation,
                        preferences=preferences,
                    ),
                    tags=[
                        city,
                        f"{days}日",
                        transportation,
                        *preferences,
                        "固定端到端",
                    ],
                )
            )
    return scenarios


FIXED_ACCEPTANCE_SCENARIOS = build_fixed_acceptance_scenarios()


def _request_signature(state: AgentState) -> tuple[object, ...]:
    request = state.request
    return (
        request.city,
        request.start_date,
        request.end_date,
        request.travel_days,
        request.transportation,
        request.accommodation,
        tuple(request.preferences),
        request.free_text_input or "",
    )


def _scenario_signature(scenario: AcceptanceScenario) -> tuple[object, ...]:
    request = scenario.request
    return (
        request.city,
        request.start_date,
        request.end_date,
        request.travel_days,
        request.transportation,
        request.accommodation,
        tuple(request.preferences),
        request.free_text_input or "",
    )


def _check(
    code: str,
    passed: bool,
    message: str,
    *,
    expected: object = None,
    actual: object = None,
) -> AcceptanceCheckResult:
    return AcceptanceCheckResult(
        code=code,
        passed=passed,
        message=message,
        expected=expected,
        actual=actual,
    )


def evaluate_acceptance_case(
    scenario: AcceptanceScenario, state: AgentState
) -> AcceptanceCaseResult:
    """对一个已完成 AgentState 执行不调用 LLM/高德的确定性验收。"""

    thresholds = scenario.thresholds
    plan = state.trip_plan
    mode = _infer_completion_mode(state)
    score = (
        state.acceptance_report.quality_score
        if state.acceptance_report is not None
        else None
    )
    route_report = state.route_quality_report
    commute_report = state.commute_report
    schedule_report = state.schedule_quality_report
    constraint_report = state.constraint_report

    minimum_attraction_count = (
        min((len(day.attractions) for day in plan.days), default=0)
        if plan is not None
        else 0
    )
    plan_identity_ok = bool(
        plan is not None
        and plan.city == scenario.request.city
        and plan.start_date == scenario.request.start_date
        and plan.end_date == scenario.request.end_date
        and len(plan.days) == scenario.request.travel_days
    )

    # 这些检查对应生产执行链路中的核心质量门，缺少报告本身也视为失败。
    checks = [
        _check(
            "session.completed",
            state.status == "completed",
            "会话必须由执行循环正常完成",
            expected="completed",
            actual=state.status,
        ),
        _check(
            "completion.mode",
            mode in thresholds.allowed_completion_modes,
            "完成模式必须是允许的完整或部分交付",
            expected=thresholds.allowed_completion_modes,
            actual=mode,
        ),
        _check(
            "quality.score",
            score is not None and score >= thresholds.min_quality_score,
            "综合质量分必须达到固定基线",
            expected=f">={thresholds.min_quality_score}",
            actual=score,
        ),
        _check(
            "plan.identity",
            plan_identity_ok,
            "城市、日期和行程天数必须与固定请求一致",
            expected={
                "city": scenario.request.city,
                "start_date": scenario.request.start_date,
                "end_date": scenario.request.end_date,
                "travel_days": scenario.request.travel_days,
            },
            actual=(
                {
                    "city": plan.city,
                    "start_date": plan.start_date,
                    "end_date": plan.end_date,
                    "travel_days": len(plan.days),
                }
                if plan is not None
                else None
            ),
        ),
        _check(
            "plan.minimum_attractions",
            minimum_attraction_count >= thresholds.min_attractions_per_day,
            "每天必须满足最低景点数量",
            expected=f">={thresholds.min_attractions_per_day}",
            actual=minimum_attraction_count,
        ),
        _check(
            "route.available",
            route_report is not None
            and route_report.unavailable_legs <= thresholds.max_unavailable_route_legs,
            "真实路线不可用分段不得超过阈值",
            expected=f"<={thresholds.max_unavailable_route_legs}",
            actual=(route_report.unavailable_legs if route_report else None),
        ),
        _check(
            "commute.segment_limit",
            commute_report is not None
            and commute_report.excessive_segment_count
            <= thresholds.max_excessive_commute_segments,
            "过长单段通勤不得超过阈值",
            expected=f"<={thresholds.max_excessive_commute_segments}",
            actual=(
                commute_report.excessive_segment_count if commute_report else None
            ),
        ),
        _check(
            "schedule.overtime",
            schedule_report is not None
            and schedule_report.total_overtime_minutes
            <= thresholds.max_schedule_overtime_minutes,
            "完整地点时间轴的总超时不得超过阈值",
            expected=f"<={thresholds.max_schedule_overtime_minutes}",
            actual=(
                schedule_report.total_overtime_minutes if schedule_report else None
            ),
        ),
        _check(
            "constraints.errors",
            constraint_report is not None
            and constraint_report.error_count <= thresholds.max_constraint_errors,
            "可执行性约束错误不得超过阈值",
            expected=f"<={thresholds.max_constraint_errors}",
            actual=(constraint_report.error_count if constraint_report else None),
        ),
        _check(
            "execution.steps",
            state.current_step <= thresholds.max_physical_steps,
            "物理执行步骤不得超过固定预算",
            expected=f"<={thresholds.max_physical_steps}",
            actual=state.current_step,
        ),
        _check(
            "execution.llm_calls",
            state.llm_call_count <= thresholds.max_llm_calls,
            "LLM 调用次数不得超过固定预算",
            expected=f"<={thresholds.max_llm_calls}",
            actual=state.llm_call_count,
        ),
    ]
    failed_codes = [item.code for item in checks if not item.passed]
    return AcceptanceCaseResult(
        case_id=scenario.case_id,
        city=scenario.request.city,
        travel_days=scenario.request.travel_days,
        transportation=scenario.request.transportation,
        status="failed" if failed_codes else "passed",
        session_id=state.session_id,
        completion_mode=mode,
        quality_level=_state_quality_level(state),
        quality_score=score,
        checks=checks,
        failed_check_codes=failed_codes,
    )


def build_fixed_acceptance_baseline(
    states: Iterable[AgentState],
    *,
    requested_limit: int,
    sampled_session_count: int,
    invalid_session_count: int = 0,
    scenarios: list[AcceptanceScenario] | None = None,
) -> FixedAcceptanceBaselineReport:
    """用每个固定场景最近一次匹配会话生成覆盖率和通过率。"""

    suite = scenarios or FIXED_ACCEPTANCE_SCENARIOS
    latest_by_signature: dict[tuple[object, ...], AgentState] = {}
    for state in states:
        signature = _request_signature(state)
        previous = latest_by_signature.get(signature)
        if previous is None or state.updated_at > previous.updated_at:
            latest_by_signature[signature] = state

    case_results: list[AcceptanceCaseResult] = []
    for scenario in suite:
        state = latest_by_signature.get(_scenario_signature(scenario))
        if state is None:
            case_results.append(
                AcceptanceCaseResult(
                    case_id=scenario.case_id,
                    city=scenario.request.city,
                    travel_days=scenario.request.travel_days,
                    transportation=scenario.request.transportation,
                    status="missing",
                    failed_check_codes=["baseline.session_missing"],
                )
            )
        else:
            case_results.append(evaluate_acceptance_case(scenario, state))

    total = len(case_results)
    covered = sum(item.status != "missing" for item in case_results)
    passed = sum(item.status == "passed" for item in case_results)
    failed = sum(item.status == "failed" for item in case_results)
    missing = total - covered

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return FixedAcceptanceBaselineReport(
        generated_at=datetime.now(timezone.utc),
        requested_limit=requested_limit,
        sampled_session_count=sampled_session_count,
        invalid_session_count=invalid_session_count,
        total_case_count=total,
        covered_case_count=covered,
        passed_case_count=passed,
        failed_case_count=failed,
        missing_case_count=missing,
        coverage_rate=rate(covered, total),
        overall_pass_rate=rate(passed, total),
        evaluated_pass_rate=rate(passed, covered),
        cases=case_results,
    )
