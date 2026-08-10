"""State models for the deterministic trip-planning agent runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.constraints import (
    ConstraintOptimizationCandidate,
    ConstraintOptimizationStatus,
    TripConstraintReport,
)
from app.routing import RouteOptimizationCandidate, RouteQualityReport
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleOptimizationCandidate, ScheduleQualityReport
from app.tools.models import ActionResult, ToolErrorType
from app.validation import TripValidationResult


CURRENT_AGENT_STATE_VERSION = 9


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class ExecutionBudget(BaseModel):
    """Persisted lifetime limits for one agent session."""

    max_steps: int = Field(default=24, ge=1)
    max_duration_seconds: float = Field(default=180.0, gt=0)
    max_tool_calls: int = Field(default=15, ge=0)
    max_llm_calls: int = Field(default=6, ge=0)
    max_repair_attempts: int = Field(default=2, ge=0)
    max_route_optimization_attempts: int = Field(default=1, ge=0)
    max_schedule_optimization_attempts: int = Field(default=1, ge=0)
    max_constraint_optimization_attempts: int = Field(default=1, ge=0)


class AgentAction(str, Enum):
    """Actions that the runtime is allowed to execute."""

    SEARCH_ATTRACTIONS = "search_attractions"
    GET_WEATHER = "get_weather"
    SEARCH_HOTELS = "search_hotels"
    GENERATE_PLAN = "generate_plan"
    VALIDATE_PLAN = "validate_plan"
    ESTIMATE_ROUTES = "estimate_routes"
    OPTIMIZE_ROUTES = "optimize_routes"
    EVALUATE_SCHEDULE = "evaluate_schedule"
    OPTIMIZE_SCHEDULE = "optimize_schedule"
    EVALUATE_CONSTRAINTS = "evaluate_constraints"
    OPTIMIZE_CONSTRAINTS = "optimize_constraints"
    REPAIR_PLAN = "repair_plan"
    FINISH = "finish"


AgentStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "max_steps_reached",
    "budget_exhausted",
]


class ActionRecord(BaseModel):
    """One deterministic decision and its execution result."""

    step: int
    action: AgentAction
    reason: str
    attempt: int = 1
    success: bool
    error: str | None = None
    tool_name: str | None = None
    error_type: ToolErrorType | None = None
    retryable: bool = False
    duration_ms: int = Field(default=0, ge=0)
    retry_delay_ms: int = Field(default=0, ge=0)
    circuit_state: str | None = None
    provider_code: str | None = None
    provider_message: str | None = None
    validation_error_count: int = Field(default=0, ge=0)
    validation_warning_count: int = Field(default=0, ge=0)
    recorded_at: datetime = Field(default_factory=utc_now)


class PlanNormalizationRecord(BaseModel):
    """Audit record for deterministic cleanup of one LLM-produced plan."""

    trigger_action: AgentAction
    removed_attraction_names: list[str] = Field(default_factory=list)
    removed_paths: list[str] = Field(default_factory=list)
    recorded_at: datetime = Field(default_factory=utc_now)


RouteOptimizationStatus = Literal[
    "not_started",
    "candidate_pending",
    "completed",
    "skipped",
]


class RouteOptimizationRecord(BaseModel):
    """Audit record for one bounded route-order optimization attempt."""

    attempt: int = Field(ge=0)
    status: Literal["accepted", "reverted", "skipped"]
    reason: str
    baseline_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    strategy: str | None = None
    changed_day_index: int | None = Field(default=None, ge=0)
    approximate_improvement_percent: float = 0.0
    actual_improvement_percent: float = 0.0
    baseline_cost: float | None = Field(default=None, ge=0)
    candidate_cost: float | None = Field(default=None, ge=0)
    recorded_at: datetime = Field(default_factory=utc_now)


ScheduleOptimizationStatus = Literal[
    "not_started",
    "candidate_pending",
    "completed",
    "skipped",
]


class ScheduleOptimizationRecord(BaseModel):
    """Audit record for one bounded cross-day schedule optimization attempt."""

    attempt: int = Field(ge=0)
    status: Literal["accepted", "reverted", "skipped"]
    reason: str
    baseline_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    strategy: str | None = None
    source_day_index: int | None = Field(default=None, ge=0)
    target_day_index: int | None = Field(default=None, ge=0)
    moved_attraction_name: str | None = None
    removed_attraction_names: list[str] = Field(default_factory=list)
    approximate_improvement_percent: float = 0.0
    actual_improvement_percent: float = 0.0
    baseline_cost: float | None = Field(default=None, ge=0)
    candidate_cost: float | None = Field(default=None, ge=0)
    recorded_at: datetime = Field(default_factory=utc_now)


class ConstraintOptimizationRecord(BaseModel):
    """Audit record for one bounded feasibility optimization attempt."""

    attempt: int = Field(ge=0)
    status: Literal["accepted", "reverted", "skipped"]
    reason: str
    baseline_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    strategy: str | None = None
    source_day_index: int | None = Field(default=None, ge=0)
    target_day_index: int | None = Field(default=None, ge=0)
    moved_attraction_name: str | None = None
    removed_attraction_names: list[str] = Field(default_factory=list)
    approximate_improvement_percent: float = 0.0
    actual_improvement_percent: float = 0.0
    baseline_cost: float | None = Field(default=None, ge=0)
    candidate_cost: float | None = Field(default=None, ge=0)
    recorded_at: datetime = Field(default_factory=utc_now)


class AgentState(BaseModel):
    """一次旅行规划的完整可变状态；该对象会整体写入 SQLite 检查点。"""

    # 会话身份、生命周期和循环边界。
    state_version: int = CURRENT_AGENT_STATE_VERSION
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    request: TripRequest
    status: AgentStatus = "pending"
    current_step: int = 0
    max_steps: int = 24
    max_repair_attempts: int = Field(default=2, ge=0)
    repair_count: int = Field(default=0, ge=0)
    finished: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime = Field(default_factory=utc_now)
    deadline_at: datetime | None = None

    # 持久化执行预算与实际消耗，恢复会话时不会重新计数。
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    tool_call_count: int = Field(default=0, ge=0)
    llm_call_count: int = Field(default=0, ge=0)
    total_retry_count: int = Field(default=0, ge=0)
    total_duration_ms: int = Field(default=0, ge=0)
    total_retry_delay_ms: int = Field(default=0, ge=0)
    budget_exhausted_reason: str | None = None

    # 每一步产生的业务数据；字段是否为空直接驱动下一动作决策。
    attractions: dict[str, Any] | None = None
    weather: dict[str, Any] | None = None
    hotels: dict[str, Any] | None = None
    trip_plan: TripPlan | None = None
    route_estimates: dict[str, Any] | None = None
    route_plan_fingerprint: str | None = None
    route_quality_report: RouteQualityReport | None = None
    route_quality_plan_fingerprint: str | None = None
    route_optimization_count: int = Field(default=0, ge=0)
    route_optimization_status: RouteOptimizationStatus = "not_started"
    route_optimization_candidate: RouteOptimizationCandidate | None = None
    route_optimization_baseline_plan: TripPlan | None = None
    route_optimization_baseline_routes: dict[str, Any] | None = None
    route_optimization_baseline_quality: RouteQualityReport | None = None
    route_optimization_baseline_fingerprint: str | None = None
    route_optimization_history: list[RouteOptimizationRecord] = Field(
        default_factory=list
    )
    schedule_quality_report: ScheduleQualityReport | None = None
    schedule_quality_plan_fingerprint: str | None = None
    schedule_optimization_count: int = Field(default=0, ge=0)
    schedule_optimization_status: ScheduleOptimizationStatus = "not_started"
    schedule_optimization_candidate: ScheduleOptimizationCandidate | None = None
    schedule_optimization_baseline_plan: TripPlan | None = None
    schedule_optimization_baseline_routes: dict[str, Any] | None = None
    schedule_optimization_baseline_route_quality: RouteQualityReport | None = None
    schedule_optimization_baseline_quality: ScheduleQualityReport | None = None
    schedule_optimization_baseline_fingerprint: str | None = None
    schedule_optimization_history: list[ScheduleOptimizationRecord] = Field(
        default_factory=list
    )
    constraint_report: TripConstraintReport | None = None
    constraint_plan_fingerprint: str | None = None
    constraint_optimization_count: int = Field(default=0, ge=0)
    constraint_optimization_status: ConstraintOptimizationStatus = "not_started"
    constraint_optimization_candidate: ConstraintOptimizationCandidate | None = None
    constraint_optimization_baseline_plan: TripPlan | None = None
    constraint_optimization_baseline_routes: dict[str, Any] | None = None
    constraint_optimization_baseline_route_quality: RouteQualityReport | None = None
    constraint_optimization_baseline_schedule: ScheduleQualityReport | None = None
    constraint_optimization_baseline_report: TripConstraintReport | None = None
    constraint_optimization_baseline_fingerprint: str | None = None
    constraint_optimization_history: list[ConstraintOptimizationRecord] = Field(
        default_factory=list
    )
    last_action_result: ActionResult | None = None
    last_validation_result: TripValidationResult | None = None
    validation_history: list[TripValidationResult] = Field(default_factory=list)
    plan_normalization_history: list[PlanNormalizationRecord] = Field(default_factory=list)

    # 可审计执行历史：动作次数、每一步记录和用户可见错误。
    attempts_by_action: dict[str, int] = Field(default_factory=dict)
    action_history: list[ActionRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_budget_compatibility(self) -> "AgentState":
        """Load older checkpoints without resetting their existing limits."""

        # 旧版本检查点没有 execution_budget 时，从旧字段补齐；新版本则以预算对象为准。
        if "execution_budget" not in self.model_fields_set:
            self.execution_budget.max_steps = self.max_steps
            self.execution_budget.max_repair_attempts = self.max_repair_attempts
        else:
            self.max_steps = self.execution_budget.max_steps
            self.max_repair_attempts = self.execution_budget.max_repair_attempts

        # 首次加载时计算绝对截止时间，恢复会话不会延长原有执行时长。
        if self.deadline_at is None:
            self.deadline_at = self.started_at + timedelta(
                seconds=self.execution_budget.max_duration_seconds
            )
        return self

    @classmethod
    def create(
        cls,
        request: TripRequest,
        *,
        max_steps: int = 24,
        max_repair_attempts: int = 2,
        max_route_optimization_attempts: int = 1,
        max_schedule_optimization_attempts: int = 1,
        max_constraint_optimization_attempts: int = 1,
        max_duration_seconds: float = 180.0,
        max_tool_calls: int = 15,
        max_llm_calls: int = 6,
        execution_budget: ExecutionBudget | None = None,
        session_id: str | None = None,
    ) -> "AgentState":
        if execution_budget is None:
            execution_budget = ExecutionBudget(
                max_steps=max_steps,
                max_duration_seconds=max_duration_seconds,
                max_tool_calls=max_tool_calls,
                max_llm_calls=max_llm_calls,
                max_repair_attempts=max_repair_attempts,
                max_route_optimization_attempts=max_route_optimization_attempts,
                max_schedule_optimization_attempts=max_schedule_optimization_attempts,
                max_constraint_optimization_attempts=max_constraint_optimization_attempts,
            )
        now = utc_now()
        values: dict[str, Any] = {
            "request": request,
            "max_steps": execution_budget.max_steps,
            "max_repair_attempts": execution_budget.max_repair_attempts,
            "execution_budget": execution_budget,
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "deadline_at": now
            + timedelta(seconds=execution_budget.max_duration_seconds),
        }
        if session_id is not None:
            values["session_id"] = session_id
        return cls(**values)

    def next_attempt(self, action: AgentAction) -> int:
        """Increment and return the lifetime attempt count for an action."""

        key = action.value
        attempt = self.attempts_by_action.get(key, 0) + 1
        self.attempts_by_action[key] = attempt
        return attempt

    def refresh_duration(self, *, now: datetime | None = None) -> None:
        """Refresh persisted wall-clock consumption without ever decreasing it."""

        current = now or utc_now()
        elapsed_ms = max(0, round((current - self.started_at).total_seconds() * 1000))
        self.total_duration_ms = max(self.total_duration_ms, elapsed_ms)

    def mark_budget_exhausted(self, reason: str) -> None:
        """Record a terminal lifetime-budget failure."""

        self.status = "budget_exhausted"
        self.budget_exhausted_reason = reason
        if reason not in self.errors:
            self.errors.append(reason)

    def touch(self) -> None:
        """Update timestamps and duration before a checkpoint."""

        self.updated_at = utc_now()
        self.refresh_duration(now=self.updated_at)
