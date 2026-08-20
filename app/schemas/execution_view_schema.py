"""面向前端结果页的轻量执行视图。

该模型只投影用户展示所需的最终行程、真实路线、时间轴和质量报告，
避免把候选池、优化基线、动作指纹及完整 action_history 长期传给前端。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.agent_runtime import AgentState, AgentStatus
from app.commute.models import CommuteConstraintReport
from app.constraints.models import TripConstraintReport
from app.providers.amap.models import RouteEstimate
from app.routing.quality import RouteQualityReport
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling.models import ScheduleQualityReport


class RouteExecutionSummary(BaseModel):
    """真实路线查询的轻量统计，不包含供应商原始响应。"""

    provider: str = "amap"
    requested_legs: int = Field(default=0, ge=0)
    evaluated_legs: int = Field(default=0, ge=0)
    truncated_legs: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)
    l1_cache_hits: int = Field(default=0, ge=0)
    l1_cache_misses: int = Field(default=0, ge=0)
    l1_cache_degraded: int = Field(default=0, ge=0)
    l1_cache_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    l2_cache_hits: int = Field(default=0, ge=0)
    l2_cache_misses: int = Field(default=0, ge=0)
    l2_cache_errors: int = Field(default=0, ge=0)
    l2_cache_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    provider_calls: int = Field(default=0, ge=0)
    provider_calls_avoided_by_l1: int = Field(default=0, ge=0)
    provider_calls_avoided_by_l2: int = Field(default=0, ge=0)
    failed_legs: int = Field(default=0, ge=0)


class TripExecutionView(BaseModel):
    """结果页所需的稳定投影；不暴露完整 AgentState。"""

    session_id: str
    status: AgentStatus
    current_step: int = Field(ge=0)
    max_steps: int = Field(ge=1)
    finished: bool
    can_resume: bool
    request: TripRequest
    trip_plan: TripPlan | None = None

    completion_mode: Literal["full", "partial"] | None = None
    quality_level: Literal["excellent", "acceptable", "degraded", "unusable"] | None = None
    quality_score: float | None = Field(default=None, ge=0, le=100)
    warnings: list[str] = Field(default_factory=list)
    last_error: str | None = None

    route_summary: RouteExecutionSummary = Field(default_factory=RouteExecutionSummary)
    route_segments: list[RouteEstimate] = Field(default_factory=list)
    route_quality_report: RouteQualityReport | None = None
    schedule_quality_report: ScheduleQualityReport | None = None
    commute_report: CommuteConstraintReport | None = None
    constraint_report: TripConstraintReport | None = None

    tool_call_count: int = Field(default=0, ge=0)
    llm_call_count: int = Field(default=0, ge=0)
    total_retry_count: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)
    updated_at: datetime

    @classmethod
    def from_agent_state(cls, state: AgentState) -> "TripExecutionView":
        """将完整检查点裁剪成前端展示视图，并兼容早期会话的缺失字段。"""

        raw_routes = state.route_estimates or {}
        route_segments: list[RouteEstimate] = []
        for raw_route in raw_routes.get("routes", []):
            try:
                route_segments.append(RouteEstimate.model_validate(raw_route))
            except (ValidationError, TypeError, ValueError):
                # 旧检查点中若存在单条损坏路线，只忽略该分段，不影响整个结果页。
                continue

        route_summary = RouteExecutionSummary(
            provider=str(raw_routes.get("provider") or "amap"),
            requested_legs=_non_negative_int(raw_routes.get("requested_legs")),
            evaluated_legs=_non_negative_int(raw_routes.get("evaluated_legs")),
            truncated_legs=_non_negative_int(raw_routes.get("truncated_legs")),
            cache_hits=_non_negative_int(raw_routes.get("cache_hits")),
            cache_misses=_non_negative_int(raw_routes.get("cache_misses")),
            l1_cache_hits=_non_negative_int(raw_routes.get("l1_cache_hits")),
            l1_cache_misses=_non_negative_int(raw_routes.get("l1_cache_misses")),
            l1_cache_degraded=_non_negative_int(
                raw_routes.get("l1_cache_degraded")
            ),
            l1_cache_hit_rate=_unit_float(raw_routes.get("l1_cache_hit_rate")),
            l2_cache_hits=_non_negative_int(raw_routes.get("l2_cache_hits")),
            l2_cache_misses=_non_negative_int(raw_routes.get("l2_cache_misses")),
            l2_cache_errors=_non_negative_int(raw_routes.get("l2_cache_errors")),
            l2_cache_hit_rate=_unit_float(raw_routes.get("l2_cache_hit_rate")),
            provider_calls=_non_negative_int(raw_routes.get("provider_calls")),
            provider_calls_avoided_by_l1=_non_negative_int(
                raw_routes.get("provider_calls_avoided_by_l1")
            ),
            provider_calls_avoided_by_l2=_non_negative_int(
                raw_routes.get("provider_calls_avoided_by_l2")
            ),
            failed_legs=_non_negative_int(raw_routes.get("failed_legs")),
        )

        acceptance = state.acceptance_report
        warnings = list(
            dict.fromkeys(
                [
                    *state.completion_warnings,
                    *(acceptance.warnings if acceptance is not None else []),
                ]
            )
        )
        quality_level = (
            acceptance.quality_level.value if acceptance is not None else None
        )
        quality_score = acceptance.quality_score if acceptance is not None else None

        return cls(
            session_id=state.session_id,
            status=state.status,
            current_step=state.current_step,
            max_steps=state.max_steps,
            finished=state.finished,
            can_resume=state.status != "completed",
            request=state.request,
            trip_plan=state.trip_plan,
            completion_mode=state.completion_mode,
            quality_level=quality_level,
            quality_score=quality_score,
            warnings=warnings,
            last_error=state.errors[-1] if state.errors else None,
            route_summary=route_summary,
            route_segments=route_segments,
            route_quality_report=state.route_quality_report,
            schedule_quality_report=state.schedule_quality_report,
            commute_report=state.commute_report,
            constraint_report=state.constraint_report,
            tool_call_count=state.tool_call_count,
            llm_call_count=state.llm_call_count,
            total_retry_count=state.total_retry_count,
            total_duration_ms=state.total_duration_ms,
            updated_at=state.updated_at,
        )


def _non_negative_int(value: object) -> int:
    """旧会话统计值异常时安全降级为 0。"""

    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _unit_float(value: object) -> float:
    """旧会话命中率异常时裁剪到 0～1。"""

    try:
        return max(0.0, min(1.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0
