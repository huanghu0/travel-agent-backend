"""确定性旅行规划智能体运行时的状态模型。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from app.agent_runtime.acceptance import (
    DEFAULT_ALLOWED_PARTIAL_ERROR_CODES,
    PartialAcceptanceReport,
)
from app.commute import (
    CommuteConstraintReport,
    CommuteReplacementCandidate,
    CommuteSupplementQuery,
)
from app.constraints import (
    ConstraintOptimizationCandidate,
    ConstraintOptimizationStatus,
    TripConstraintReport,
)
from app.plan_content import ContentRefillCandidate
from app.rag.models import RagContext
from app.routing import RouteOptimizationCandidate, RouteQualityReport
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleOptimizationCandidate, ScheduleQualityReport
from app.tools.models import ActionResult, ToolErrorType
from app.validation import TripValidationResult


CURRENT_AGENT_STATE_VERSION = 17


def utc_now() -> datetime:
    """返回带时区信息的 UTC 时间。"""

    return datetime.now(timezone.utc)


class ExecutionBudget(BaseModel):
    """单个智能体会话需要持久化的生命周期预算。"""

    max_steps: int = Field(default=24, ge=1)
    max_duration_seconds: float = Field(default=600.0, gt=0)
    max_tool_calls: int = Field(default=30, ge=0)
    max_llm_calls: int = Field(default=6, ge=0)
    max_repair_attempts: int = Field(default=2, ge=0)
    max_route_optimization_attempts: int = Field(default=1, ge=0)
    max_schedule_optimization_attempts: int = Field(default=1, ge=0)
    max_constraint_optimization_attempts: int = Field(default=1, ge=0)
    max_content_refill_attempts: int = Field(default=2, ge=0)
    max_commute_replacement_attempts: int = Field(default=2, ge=0)
    max_commute_supplement_searches: int = Field(default=2, ge=0)
    minimum_total_attractions: int = Field(default=0, ge=0)
    # 执行循环收敛预算：限制重复动作输入和连续无收益步骤。
    max_repeated_action_inputs: int = Field(default=1, ge=1)
    max_no_progress_steps: int = Field(default=3, ge=1)
    # 单个物理步骤最多吸收的确定性本地动作，防止压缩批次内部无限跳转。
    max_local_actions_per_step: int = Field(default=8, ge=1)
    # 部分可接受结果策略：只允许非关键问题降级交付，核心路线与约束仍须满足。
    partial_acceptance_enabled: bool = True
    partial_acceptance_min_score: float = Field(default=70.0, ge=0.0, le=100.0)
    partial_acceptance_max_validation_errors: int = Field(default=2, ge=0)
    partial_acceptance_max_schedule_overtime_minutes: int = Field(default=60, ge=0)
    partial_acceptance_max_unavailable_route_legs: int = Field(default=0, ge=0)
    partial_acceptance_max_excessive_commute_segments: int = Field(default=0, ge=0)
    partial_acceptance_max_constraint_errors: int = Field(default=0, ge=0)
    partial_acceptance_min_attractions_per_day: int = Field(default=0, ge=0)
    partial_acceptance_allowed_error_codes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_PARTIAL_ERROR_CODES)
    )


class AgentAction(str, Enum):
    """运行时允许执行的动作白名单。"""

    SEARCH_ATTRACTIONS = "search_attractions"
    GET_WEATHER = "get_weather"
    SEARCH_HOTELS = "search_hotels"
    SEARCH_RESTAURANTS = "search_restaurants"
    GENERATE_PLAN = "generate_plan"
    VALIDATE_PLAN = "validate_plan"
    ESTIMATE_ROUTES = "estimate_routes"
    OPTIMIZE_ROUTES = "optimize_routes"
    EVALUATE_COMMUTE = "evaluate_commute"
    REPLACE_REMOTE_ATTRACTION = "replace_remote_attraction"
    SUPPLEMENT_ATTRACTIONS = "supplement_attractions"
    EVALUATE_SCHEDULE = "evaluate_schedule"
    OPTIMIZE_SCHEDULE = "optimize_schedule"
    EVALUATE_CONSTRAINTS = "evaluate_constraints"
    OPTIMIZE_CONSTRAINTS = "optimize_constraints"
    REFILL_ATTRACTIONS = "refill_attractions"
    REBUILD_PLAN_CONTENT = "rebuild_plan_content"
    REPAIR_PLAN = "repair_plan"
    FINISH = "finish"


AgentStatus = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "max_steps_reached",
    "budget_exhausted",
    "convergence_stopped",
    "cancelled",
]


class ActionRecord(BaseModel):
    """一次确定性决策及其执行结果审计记录。"""

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
    # 收敛审计字段由执行循环在动作完成后补写。
    input_fingerprint: str | None = None
    state_fingerprint_before: str | None = None
    state_fingerprint_after: str | None = None
    made_progress: bool | None = None
    # 状态跳转压缩审计：被吸收到同一物理步骤的子动作仍保留独立记录。
    compressed: bool = False
    batch_root_action: AgentAction | None = None
    batch_index: int = Field(default=0, ge=0)
    compressed_actions: list[AgentAction] = Field(default_factory=list)
    recorded_at: datetime = Field(default_factory=utc_now)


class ConvergenceRecord(BaseModel):
    """一次成功动作的收敛判断记录。"""

    step: int
    action: AgentAction
    input_fingerprint: str
    state_fingerprint_before: str
    state_fingerprint_after: str
    made_progress: bool
    reason: str
    recorded_at: datetime = Field(default_factory=utc_now)


class PlanNormalizationRecord(BaseModel):
    """对 LLM 生成行程执行确定性清理的审计记录。"""

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
    """一次有界路线顺序优化尝试的审计记录。"""

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
    """一次有界跨日时间轴优化尝试的审计记录。"""

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
    """一次有界可执行性冲突优化尝试的审计记录。"""

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


ContentRefillStatus = Literal[
    "not_started",
    "candidate_pending",
    "completed",
    "skipped",
]


CommuteOptimizationStatus = Literal[
    "not_started",
    "supplement_needed",
    "candidate_pending",
    "completed",
    "skipped",
]


class CommuteSupplementRecord(BaseModel):
    """一次有界高德周边候选补充搜索的审计记录。"""

    attempt: int = Field(ge=1)
    status: Literal["completed", "empty", "failed"]
    reason: str
    target_attraction_name: str
    day_index: int = Field(ge=0)
    attraction_index: int = Field(ge=0)
    anchor_names: list[str] = Field(default_factory=list)
    center_longitude: float = Field(ge=-180, le=180)
    center_latitude: float = Field(ge=-90, le=90)
    radius_meters: int = Field(ge=100, le=50000)
    received_candidates: int = Field(default=0, ge=0)
    added_candidates: int = Field(default=0, ge=0)
    final_candidates: int = Field(default=0, ge=0)
    error: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)


class CommuteReplacementRecord(BaseModel):
    """一次通勤超限景点替换及复验结果的审计记录。"""

    attempt: int = Field(ge=0)
    status: Literal["accepted", "reverted", "skipped"]
    reason: str
    day_index: int | None = Field(default=None, ge=0)
    attraction_index: int | None = Field(default=None, ge=0)
    replaced_attraction_name: str | None = None
    replacement_attraction_name: str | None = None
    replacement_attraction_id: str | None = None
    baseline_excessive_segments: int = Field(default=0, ge=0)
    candidate_excessive_segments: int = Field(default=0, ge=0)
    baseline_max_duration_seconds: int = Field(default=0, ge=0)
    candidate_max_duration_seconds: int = Field(default=0, ge=0)
    baseline_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)


class ContentRefillRecord(BaseModel):
    """一次最低景点数量回填候选及最终结果的审计记录。"""

    attempt: int = Field(ge=0)
    status: Literal["accepted", "reverted", "skipped"]
    reason: str
    added_attraction_names: list[str] = Field(default_factory=list)
    added_attraction_ids: list[str] = Field(default_factory=list)
    target_day_indices: list[int] = Field(default_factory=list)
    baseline_attraction_count: int = Field(default=0, ge=0)
    candidate_attraction_count: int = Field(default=0, ge=0)
    baseline_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)


class AgentState(BaseModel):
    """一次旅行规划的完整可变状态；该对象会整体写入 SQLite 检查点。"""

    _checkpoint_persisted: bool = PrivateAttr(default=False)

    # 会话身份、生命周期和循环边界。
    state_version: int = CURRENT_AGENT_STATE_VERSION
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    user_id: str | None = None
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
    rag_context: RagContext | None = None
    # 最终行程锚点周边的真实高德餐厅候选。
    restaurants: dict[str, Any] | None = None
    restaurant_plan_fingerprint: str | None = None
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
    commute_report: CommuteConstraintReport | None = None
    commute_plan_fingerprint: str | None = None
    commute_replacement_count: int = Field(default=0, ge=0)
    commute_optimization_status: CommuteOptimizationStatus = "not_started"
    commute_candidate: CommuteReplacementCandidate | None = None
    commute_baseline_plan: TripPlan | None = None
    commute_baseline_routes: dict[str, Any] | None = None
    commute_baseline_route_quality: RouteQualityReport | None = None
    commute_baseline_report: CommuteConstraintReport | None = None
    commute_baseline_schedule: ScheduleQualityReport | None = None
    commute_baseline_constraint_report: TripConstraintReport | None = None
    commute_baseline_route_fingerprint: str | None = None
    commute_baseline_constraint_fingerprint: str | None = None
    commute_excluded_candidate_identities: list[str] = Field(default_factory=list)
    commute_supplement_search_count: int = Field(default=0, ge=0)
    commute_supplement_query: CommuteSupplementQuery | None = None
    commute_supplement_history: list[CommuteSupplementRecord] = Field(
        default_factory=list
    )
    commute_replacement_history: list[CommuteReplacementRecord] = Field(
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
    content_refill_count: int = Field(default=0, ge=0)
    content_refill_status: ContentRefillStatus = "not_started"
    content_refill_candidate: ContentRefillCandidate | None = None
    content_refill_baseline_plan: TripPlan | None = None
    content_refill_baseline_routes: dict[str, Any] | None = None
    content_refill_baseline_route_quality: RouteQualityReport | None = None
    content_refill_baseline_schedule: ScheduleQualityReport | None = None
    content_refill_baseline_constraint_report: TripConstraintReport | None = None
    content_refill_baseline_route_fingerprint: str | None = None
    content_refill_baseline_constraint_fingerprint: str | None = None
    content_refill_excluded_identities: list[str] = Field(default_factory=list)
    content_refill_history: list[ContentRefillRecord] = Field(default_factory=list)
    plan_consistency_fingerprint: str | None = None
    plan_consistency_rebuild_count: int = Field(default=0, ge=0)
    last_action_result: ActionResult | None = None
    last_validation_result: TripValidationResult | None = None
    validation_history: list[TripValidationResult] = Field(default_factory=list)
    # 最终质量分级和降级原因会随 AgentState 持久化，供前端展示和后续复盘。
    acceptance_report: PartialAcceptanceReport | None = None
    completion_mode: Literal["full", "partial"] | None = None
    completion_warnings: list[str] = Field(default_factory=list)
    plan_normalization_history: list[PlanNormalizationRecord] = Field(default_factory=list)

    # 执行循环收敛状态：完整评估输入、成功动作输入和无收益连续次数。
    evaluation_input_fingerprints: dict[str, str] = Field(default_factory=dict)
    successful_action_inputs: dict[str, int] = Field(default_factory=dict)
    no_progress_streak: int = Field(default=0, ge=0)
    no_progress_total: int = Field(default=0, ge=0)
    convergence_history: list[ConvergenceRecord] = Field(default_factory=list)
    convergence_terminated_reason: str | None = None

    # 状态跳转压缩指标：批次数与被吸收的逻辑动作数会随检查点持久化。
    local_action_batch_count: int = Field(default=0, ge=0)
    compressed_local_action_count: int = Field(default=0, ge=0)

    # 可审计执行历史：动作次数、每一步记录和用户可见错误。
    attempts_by_action: dict[str, int] = Field(default_factory=dict)
    action_history: list[ActionRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_budget_compatibility(self) -> "AgentState":
        """加载旧版本检查点，同时保留其已有生命周期预算。"""

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
        max_content_refill_attempts: int = 2,
        max_commute_replacement_attempts: int = 2,
        max_commute_supplement_searches: int = 2,
        minimum_total_attractions: int = 0,
        max_repeated_action_inputs: int = 1,
        max_no_progress_steps: int = 3,
        max_local_actions_per_step: int = 8,
        partial_acceptance_enabled: bool = True,
        partial_acceptance_min_score: float = 70.0,
        partial_acceptance_max_validation_errors: int = 2,
        partial_acceptance_max_schedule_overtime_minutes: int = 60,
        partial_acceptance_max_unavailable_route_legs: int = 0,
        partial_acceptance_max_excessive_commute_segments: int = 0,
        partial_acceptance_max_constraint_errors: int = 0,
        partial_acceptance_min_attractions_per_day: int = 0,
        partial_acceptance_allowed_error_codes: list[str] | None = None,
        max_duration_seconds: float = 600.0,
        max_tool_calls: int = 30,
        max_llm_calls: int = 6,
        execution_budget: ExecutionBudget | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
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
                max_content_refill_attempts=max_content_refill_attempts,
                max_commute_replacement_attempts=max_commute_replacement_attempts,
                max_commute_supplement_searches=max_commute_supplement_searches,
                minimum_total_attractions=minimum_total_attractions,
                max_repeated_action_inputs=max_repeated_action_inputs,
                max_no_progress_steps=max_no_progress_steps,
                max_local_actions_per_step=max_local_actions_per_step,
                partial_acceptance_enabled=partial_acceptance_enabled,
                partial_acceptance_min_score=partial_acceptance_min_score,
                partial_acceptance_max_validation_errors=(
                    partial_acceptance_max_validation_errors
                ),
                partial_acceptance_max_schedule_overtime_minutes=(
                    partial_acceptance_max_schedule_overtime_minutes
                ),
                partial_acceptance_max_unavailable_route_legs=(
                    partial_acceptance_max_unavailable_route_legs
                ),
                partial_acceptance_max_excessive_commute_segments=(
                    partial_acceptance_max_excessive_commute_segments
                ),
                partial_acceptance_max_constraint_errors=(
                    partial_acceptance_max_constraint_errors
                ),
                partial_acceptance_min_attractions_per_day=(
                    partial_acceptance_min_attractions_per_day
                ),
                partial_acceptance_allowed_error_codes=(
                    partial_acceptance_allowed_error_codes
                    if partial_acceptance_allowed_error_codes is not None
                    else list(DEFAULT_ALLOWED_PARTIAL_ERROR_CODES)
                ),
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
        if user_id is not None:
            values["user_id"] = user_id
        return cls(**values)

    def next_attempt(self, action: AgentAction) -> int:
        """增加并返回某个动作在整个会话生命周期中的尝试次数。"""

        key = action.value
        attempt = self.attempts_by_action.get(key, 0) + 1
        self.attempts_by_action[key] = attempt
        return attempt

    def refresh_duration(self, *, now: datetime | None = None) -> None:
        """刷新持久化的实际运行耗时，并保证累计值不会倒退。"""

        current = now or utc_now()
        elapsed_ms = max(0, round((current - self.started_at).total_seconds() * 1000))
        self.total_duration_ms = max(self.total_duration_ms, elapsed_ms)

    def mark_budget_exhausted(self, reason: str) -> None:
        """记录不可继续执行的生命周期预算耗尽状态。"""

        self.status = "budget_exhausted"
        self.budget_exhausted_reason = reason
        if reason not in self.errors:
            self.errors.append(reason)

    def touch(self) -> None:
        """保存检查点前更新时间戳和累计运行时长。"""

        self.updated_at = utc_now()
        self.refresh_duration(now=self.updated_at)

    @property
    def checkpoint_persisted(self) -> bool:
        return self._checkpoint_persisted

    def mark_checkpoint_persisted(self) -> None:
        """标记首次检查点已提交；该运行时标记不会写入状态 JSON。"""

        self._checkpoint_persisted = True
