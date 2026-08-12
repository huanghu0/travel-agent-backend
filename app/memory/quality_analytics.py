"""从持久化 AgentState 生成交付质量、资源消耗和警告代码基线。"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable, Literal

from app.agent_runtime.state import AgentState, AgentStatus
from app.memory.models import (
    QualityAggregate,
    QualityBaselineReport,
    QualityDimensionBaseline,
    QualityIssueStats,
)

CompletionMode = Literal["full", "partial"]
QualityLevel = Literal["excellent", "acceptable", "degraded", "unusable"]

_TERMINAL_FAILURE_STATUSES = {
    "failed",
    "max_steps_reached",
    "budget_exhausted",
    "convergence_stopped",
}


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _safe_average(total: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return round(total / count, 2)


def infer_completion_mode(state: AgentState) -> CompletionMode | None:
    """兼容旧检查点：历史 completed 会话未记录模式时按完整完成统计。"""

    if state.status != "completed":
        return None
    if state.completion_mode == "partial":
        return "partial"
    return "full"


def state_quality_level(state: AgentState) -> QualityLevel | None:
    report = state.acceptance_report
    if report is None:
        return None
    return report.quality_level.value


def state_issue_codes(state: AgentState) -> list[str]:
    """返回去重后的最终问题代码；同一会话同一代码只计一次。"""

    report = state.acceptance_report
    if report is None:
        return []
    return list(dict.fromkeys(code for code in report.unresolved_issue_codes if code))


def _aggregate(states: list[AgentState]) -> QualityAggregate:
    full_count = 0
    partial_count = 0
    failed_count = 0
    in_progress_count = 0
    scores: list[float] = []

    for state in states:
        mode = infer_completion_mode(state)
        if mode == "full":
            full_count += 1
        elif mode == "partial":
            partial_count += 1
        elif state.status in _TERMINAL_FAILURE_STATUSES:
            failed_count += 1
        else:
            in_progress_count += 1

        if state.acceptance_report is not None:
            scores.append(state.acceptance_report.quality_score)

    total = len(states)
    successful = full_count + partial_count
    # 每次部分接受都确定性地跳过一次原本可能进入的 LLM 修复动作。
    # 该指标明确标记为 estimated，避免把策略收益误解为真实计费账单。
    avoided_repairs = partial_count
    return QualityAggregate(
        session_count=total,
        full_completed_count=full_count,
        partial_completed_count=partial_count,
        failed_count=failed_count,
        in_progress_count=in_progress_count,
        full_completion_rate=_safe_rate(full_count, total),
        partial_completion_rate=_safe_rate(partial_count, total),
        success_rate=_safe_rate(successful, total),
        failure_rate=_safe_rate(failed_count, total),
        scored_session_count=len(scores),
        avg_quality_score=_safe_average(sum(scores), len(scores)),
        avg_physical_steps=_safe_average(
            sum(state.current_step for state in states), total
        ),
        avg_logical_actions=_safe_average(
            sum(len(state.action_history) for state in states), total
        ),
        avg_tool_calls=_safe_average(
            sum(state.tool_call_count for state in states), total
        ),
        avg_llm_calls=_safe_average(
            sum(state.llm_call_count for state in states), total
        ),
        avg_duration_ms=_safe_average(
            sum(state.total_duration_ms for state in states), total
        ),
        estimated_avoided_llm_repair_calls=avoided_repairs,
        estimated_avoided_repair_steps=avoided_repairs,
    )


def _dimension_rows(
    grouped: dict[str, list[AgentState]],
    *,
    dimension: Literal["city", "travel_days", "transportation", "quality_level"],
) -> list[QualityDimensionBaseline]:
    rows = [
        QualityDimensionBaseline(
            dimension=dimension,
            value=value,
            **_aggregate(group_states).model_dump(),
        )
        for value, group_states in grouped.items()
    ]
    rows.sort(key=lambda item: (-item.session_count, item.value))
    return rows


def build_quality_baseline(
    states: Iterable[AgentState],
    *,
    requested_limit: int,
    matching_session_count: int,
    sampled_row_count: int,
    invalid_session_count: int = 0,
    status_filter: AgentStatus | None = None,
    city_filter: str | None = None,
    travel_days_filter: int | None = None,
    transportation_filter: str | None = None,
    completion_mode_filter: CompletionMode | None = None,
    quality_level_filter: QualityLevel | None = None,
    top_n: int = 20,
) -> QualityBaselineReport:
    """聚合最近会话，输出完整/部分完成率、质量分和资源消耗。"""

    state_list = list(states)
    city_groups: dict[str, list[AgentState]] = defaultdict(list)
    day_groups: dict[str, list[AgentState]] = defaultdict(list)
    transport_groups: dict[str, list[AgentState]] = defaultdict(list)
    quality_groups: dict[str, list[AgentState]] = defaultdict(list)
    issue_counts: Counter[str] = Counter()
    partial_issue_counts: Counter[str] = Counter()

    for state in state_list:
        # 步骤 1：按请求维度分组，为城市、天数和交通方式生成可比较基线。
        city_groups[state.request.city].append(state)
        day_groups[str(state.request.travel_days)].append(state)
        transport_groups[state.request.transportation].append(state)
        level = state_quality_level(state)
        if level is not None:
            quality_groups[level].append(state)

        # 步骤 2：问题代码按“出现会话数”统计，避免同一会话重复告警放大数据。
        codes = state_issue_codes(state)
        issue_counts.update(codes)
        if infer_completion_mode(state) == "partial":
            partial_issue_counts.update(codes)

    common_issues = [
        QualityIssueStats(
            issue_code=code,
            occurrence_count=count,
            session_count=count,
            partial_session_count=partial_issue_counts[code],
        )
        for code, count in issue_counts.items()
    ]
    common_issues.sort(key=lambda item: (-item.session_count, item.issue_code))

    safe_top_n = max(1, top_n)
    return QualityBaselineReport(
        generated_at=datetime.now(timezone.utc),
        requested_limit=requested_limit,
        matching_session_count=matching_session_count,
        sampled_row_count=sampled_row_count,
        analyzed_session_count=len(state_list),
        invalid_session_count=invalid_session_count,
        truncated=matching_session_count > sampled_row_count,
        status_filter=status_filter,
        city_filter=city_filter,
        travel_days_filter=travel_days_filter,
        transportation_filter=transportation_filter,
        completion_mode_filter=completion_mode_filter,
        quality_level_filter=quality_level_filter,
        overall=_aggregate(state_list),
        cities=_dimension_rows(city_groups, dimension="city"),
        travel_days=_dimension_rows(day_groups, dimension="travel_days"),
        transportation=_dimension_rows(transport_groups, dimension="transportation"),
        quality_levels=_dimension_rows(quality_groups, dimension="quality_level"),
        common_issue_codes=common_issues[:safe_top_n],
    )
