"""确定性判断当前行程是否已经达到“部分可接受”交付标准。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.validation import TripValidationResult, ValidationSeverity

if TYPE_CHECKING:
    from app.agent_runtime.state import AgentState


DEFAULT_ALLOWED_PARTIAL_ERROR_CODES = (
    "plan.empty_suggestions",
    "schedule.daily_overtime",
)


class PlanQualityLevel(str, Enum):
    """面向前端和审计日志的最终行程质量等级。"""

    EXCELLENT = "excellent"
    ACCEPTABLE = "acceptable"
    DEGRADED = "degraded"
    UNUSABLE = "unusable"


class PartialAcceptanceReport(BaseModel):
    """一次最低可接受标准评估的结构化结果。"""

    accepted: bool = False
    partial: bool = False
    quality_level: PlanQualityLevel = PlanQualityLevel.UNUSABLE
    quality_score: float = Field(default=0.0, ge=0.0, le=100.0)
    reason: str = ""
    core_checks: dict[str, bool] = Field(default_factory=dict)
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unresolved_issue_codes: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class PartialAcceptancePolicy:
    """使用持久化预算阈值，对最终行程执行无 LLM 的确定性分级。"""

    @staticmethod
    def evaluate(
        state: "AgentState",
        validation: TripValidationResult,
    ) -> PartialAcceptanceReport:
        """判断当前结果能否在保留非关键警告的情况下提前完成。"""

        plan = state.trip_plan
        budget = state.execution_budget
        if plan is None:
            return PartialAcceptanceReport(
                reason="尚未生成结构化行程",
                blocking_reasons=["缺少旅行计划"],
            )

        # 步骤 1：检查城市、日期、天数和每天最低景点数等核心交付结构。
        identity_ok = (
            plan.city == state.request.city
            and plan.start_date == state.request.start_date
            and plan.end_date == state.request.end_date
        )
        day_count_ok = (
            len(plan.days) == state.request.travel_days
            and len(plan.days) > 0
        )
        minimum_per_day = budget.partial_acceptance_min_attractions_per_day
        attraction_coverage_ok = all(
            len(day.attractions) >= minimum_per_day for day in plan.days
        )

        # 步骤 2：检查真实路线、时间轴、通勤和可执行性约束是否处于安全阈值内。
        route_report = state.route_quality_report
        schedule_report = state.schedule_quality_report
        commute_report = state.commute_report
        constraint_report = state.constraint_report
        routes_ok = bool(
            route_report is not None
            and route_report.unavailable_legs
            <= budget.partial_acceptance_max_unavailable_route_legs
        )
        schedule_ok = bool(
            schedule_report is not None
            and schedule_report.total_overtime_minutes
            <= budget.partial_acceptance_max_schedule_overtime_minutes
        )
        commute_ok = bool(
            commute_report is not None
            and commute_report.excessive_segment_count
            <= budget.partial_acceptance_max_excessive_commute_segments
        )
        constraints_ok = bool(
            constraint_report is not None
            and constraint_report.error_count
            <= budget.partial_acceptance_max_constraint_errors
        )

        errors = [
            issue
            for issue in validation.issues
            if issue.severity is ValidationSeverity.ERROR
        ]
        warnings = [
            issue
            for issue in validation.issues
            if issue.severity is ValidationSeverity.WARNING
        ]
        allowed_codes = set(budget.partial_acceptance_allowed_error_codes)
        disallowed_errors = [issue for issue in errors if issue.code not in allowed_codes]
        validation_ok = (
            len(errors) <= budget.partial_acceptance_max_validation_errors
            and not disallowed_errors
        )

        core_checks = {
            "plan_identity": identity_ok,
            "day_count": day_count_ok,
            "minimum_attractions_per_day": attraction_coverage_ok,
            "route_availability": routes_ok,
            "schedule_overtime": schedule_ok,
            "commute_limit": commute_ok,
            "execution_constraints": constraints_ok,
            "validation_allowlist": validation_ok,
        }
        blocking_reasons: list[str] = []
        if not identity_ok:
            blocking_reasons.append("城市或起止日期与用户请求不一致")
        if not day_count_ok:
            blocking_reasons.append("行程天数不完整")
        if not attraction_coverage_ok:
            blocking_reasons.append(
                f"至少一天少于 {minimum_per_day} 个景点"
            )
        if not routes_ok:
            blocking_reasons.append("不可用路线分段超过允许上限")
        if not schedule_ok:
            blocking_reasons.append("日程超时超过允许上限")
        if not commute_ok:
            blocking_reasons.append("过长通勤分段超过允许上限")
        if not constraints_ok:
            blocking_reasons.append("仍存在阻断执行的约束错误")
        if not validation_ok:
            codes = ", ".join(sorted({item.code for item in disallowed_errors}))
            blocking_reasons.append(
                f"存在不可降级接受的校验错误：{codes or '错误数量超过上限'}"
            )

        # 步骤 3：综合已有四类质量分数和校验问题，生成稳定的 0～100 分。
        route_score = route_report.quality_score if route_report is not None else 0.0
        schedule_score = (
            schedule_report.quality_score if schedule_report is not None else 0.0
        )
        constraint_score = (
            constraint_report.quality_score if constraint_report is not None else 0.0
        )
        validation_score = max(
            0.0,
            100.0 - len(errors) * 12.0 - len(warnings) * 2.0,
        )
        content_score = 100.0 * sum(
            int(len(day.attractions) >= minimum_per_day) for day in plan.days
        ) / max(1, len(plan.days))
        quality_score = round(
            (
                route_score
                + schedule_score
                + constraint_score
                + validation_score
                + content_score
            )
            / 5.0,
            2,
        )

        unresolved = list(dict.fromkeys(issue.code for issue in validation.issues))
        warning_messages = list(
            dict.fromkeys(issue.message for issue in [*errors, *warnings])
        )
        core_ok = all(core_checks.values())

        # 完整校验通过时始终允许完成；分数只用于区分 excellent/acceptable。
        if validation.valid:
            level = (
                PlanQualityLevel.EXCELLENT
                if quality_score >= 90.0 and not warnings
                else PlanQualityLevel.ACCEPTABLE
            )
            return PartialAcceptanceReport(
                accepted=True,
                partial=False,
                quality_level=level,
                quality_score=quality_score,
                reason="行程已通过完整校验",
                core_checks=core_checks,
                warnings=warning_messages,
                unresolved_issue_codes=unresolved,
            )

        accepted = bool(
            budget.partial_acceptance_enabled
            and core_ok
            and quality_score >= budget.partial_acceptance_min_score
        )
        if accepted:
            return PartialAcceptanceReport(
                accepted=True,
                partial=True,
                quality_level=PlanQualityLevel.ACCEPTABLE,
                quality_score=quality_score,
                reason="核心可执行性约束已满足，仅保留允许降级的非关键问题",
                core_checks=core_checks,
                warnings=warning_messages,
                unresolved_issue_codes=unresolved,
            )

        # 结构完整但存在阻断问题时标记 degraded；核心结构本身损坏则为 unusable。
        structural_ok = identity_ok and day_count_ok
        return PartialAcceptanceReport(
            accepted=False,
            partial=False,
            quality_level=(
                PlanQualityLevel.DEGRADED
                if structural_ok
                else PlanQualityLevel.UNUSABLE
            ),
            quality_score=quality_score,
            reason="当前结果尚未达到最低可接受标准",
            core_checks=core_checks,
            blocking_reasons=blocking_reasons,
            warnings=warning_messages,
            unresolved_issue_codes=unresolved,
        )
