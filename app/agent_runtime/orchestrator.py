"""旅行规划确定性运行时：根据 AgentState 选动作，并通过工具白名单有界执行。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agent_runtime.acceptance import (
    DEFAULT_ALLOWED_PARTIAL_ERROR_CODES,
    PartialAcceptancePolicy,
)
from app.agent_runtime.checkpoint_policy import CheckpointPolicy
from app.agent_runtime.convergence import (
    action_input_fingerprint,
    business_state_fingerprint,
    commute_input_fingerprint,
    constraint_input_fingerprint,
    route_quality_input_fingerprint,
    schedule_input_fingerprint,
    validation_input_fingerprint,
)
from app.agent_runtime.exceptions import (
    AgentActionError,
    AgentBudgetExceededError,
    AgentCheckpointError,
    AgentConvergenceError,
    AgentMaxStepsError,
)
from app.agent_runtime.execution_policy import CircuitBreaker, ExecutionPolicy
from app.agent_runtime.state import (
    ActionRecord,
    AgentAction,
    AgentState,
    CommuteReplacementRecord,
    CommuteSupplementRecord,
    ConstraintOptimizationRecord,
    ContentRefillRecord,
    ConvergenceRecord,
    PlanNormalizationRecord,
    RouteOptimizationRecord,
    ScheduleOptimizationRecord,
)
from app.core.config import settings
from app.commute import (
    CommuteCandidatePoolSupplementer,
    CommuteConstraintEvaluator,
    RemoteAttractionReplacementOptimizer,
)
from app.constraints import (
    ConstraintEvaluator,
    DeterministicConstraintOptimizer,
    constraint_plan_fingerprint,
)
from app.persistence.interfaces import AgentStateStore
from app.plan_content import (
    MinimumAttractionRefillOptimizer,
    TripPlanConsistencyRebuilder,
    attraction_identity as content_attraction_identity,
    build_restaurant_search_anchors,
    count_attractions,
    plan_content_source_fingerprint,
    restaurant_search_source_fingerprint,
)
from app.providers.amap.models import (
    NearbyAttractionSearchResult,
    RestaurantSearchResult,
    RouteEstimateResult,
)
from app.routing import (
    DeterministicRouteOptimizer,
    build_route_legs,
    evaluate_route_quality,
    is_route_quality_improvement,
    plan_route_fingerprint,
    route_quality_improvement_percent,
)
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import (
    DeterministicScheduleOptimizer,
    ScheduleTimelineEvaluator,
)
from app.task_runtime.context import (
    notify_action_completed,
    notify_action_started,
    raise_if_task_cancelled,
    raise_if_task_lease_lost,
    sleep_with_task_cancellation,
)
from app.tools.models import ActionResult, ToolErrorType
from app.tools.registry import ToolRegistry
from app.tools.trip_registry import GeneratePlanResult, build_trip_tool_registry
from app.validation import TripPlanValidator, remove_duplicate_attractions


_ACTION_REASONS = {
    AgentAction.SEARCH_ATTRACTIONS: "景点数据尚未获取",
    AgentAction.GET_WEATHER: "天气数据尚未获取",
    AgentAction.SEARCH_HOTELS: "酒店数据尚未获取",
    AgentAction.SEARCH_RESTAURANTS: "最终地点时间轴已稳定，需要查询真实餐饮候选",
    AgentAction.GENERATE_PLAN: "基础数据已就绪，需要生成行程",
    AgentAction.ESTIMATE_ROUTES: "行程已生成，需要查询相邻景点的真实路线",
    AgentAction.EVALUATE_COMMUTE: "\u771f\u5b9e\u8def\u7ebf\u5df2\u5c31\u7eea\uff0c\u9700\u8981\u8bc4\u4f30\u5355\u6bb5\u901a\u52e4\u4e0a\u9650",
    AgentAction.REPLACE_REMOTE_ATTRACTION: "\u5b58\u5728\u8fc7\u957f\u901a\u52e4\uff0c\u9700\u8981\u7528\u8fd1\u8ddd\u79bb\u672a\u4f7f\u7528\u666f\u70b9\u8fdb\u884c\u6709\u754c\u66ff\u6362",
    AgentAction.SUPPLEMENT_ATTRACTIONS: "本地候选不足，需要查询高德附近景点补充候选池",
    AgentAction.OPTIMIZE_ROUTES: "路线质量较低，需要执行有界确定性排序优化",
    AgentAction.EVALUATE_SCHEDULE: "行程路线已就绪，需要执行确定性时间轴评估",
    AgentAction.OPTIMIZE_SCHEDULE: "日程存在超时，需要执行有界确定性跨日优化",
    AgentAction.EVALUATE_CONSTRAINTS: "\u65f6\u95f4\u8f74\u5df2\u5c31\u7eea\uff0c\u9700\u8981\u8bc4\u4f30\u884c\u7a0b\u53ef\u6267\u884c\u6027\u7ea6\u675f",
    AgentAction.OPTIMIZE_CONSTRAINTS: "\u884c\u7a0b\u5b58\u5728\u53ef\u4fee\u590d\u7ea6\u675f\u51b2\u7a81\uff0c\u9700\u8981\u6267\u884c\u6709\u754c\u786e\u5b9a\u6027\u4f18\u5316",
    AgentAction.REFILL_ATTRACTIONS: "\u6700\u7ec8\u666f\u70b9\u6570\u91cf\u4e0d\u8db3\uff0c\u9700\u8981\u4ece\u8fd1\u8ddd\u79bb\u672a\u4f7f\u7528\u5019\u9009\u4e2d\u786e\u5b9a\u6027\u56de\u586b",
    AgentAction.REBUILD_PLAN_CONTENT: "\u666f\u70b9\u4e0e\u8def\u7ebf\u5df2\u7ecf\u7a33\u5b9a\uff0c\u9700\u8981\u91cd\u5efa\u63cf\u8ff0\u3001\u9910\u996e\u3001\u4ea4\u901a\u548c\u9884\u7b97",
    AgentAction.VALIDATE_PLAN: "行程已生成，需要执行确定性语义校验",
    AgentAction.REPAIR_PLAN: "行程未通过校验，需要根据结构化问题修复",
    AgentAction.FINISH: "行程已通过校验，结束执行",
}


# 只有不调用 HTTP/LLM 的确定性动作才能被吸收到同一物理步骤。
# 每个逻辑动作仍保留 ActionRecord，不会丢失 SQLite 审计信息。
_COMPRESSIBLE_LOCAL_ACTIONS = frozenset(
    {
        AgentAction.OPTIMIZE_ROUTES,
        AgentAction.EVALUATE_COMMUTE,
        AgentAction.REPLACE_REMOTE_ATTRACTION,
        AgentAction.EVALUATE_SCHEDULE,
        AgentAction.OPTIMIZE_SCHEDULE,
        AgentAction.EVALUATE_CONSTRAINTS,
        AgentAction.OPTIMIZE_CONSTRAINTS,
        AgentAction.REFILL_ATTRACTIONS,
        AgentAction.REBUILD_PLAN_CONTENT,
        AgentAction.VALIDATE_PLAN,
        AgentAction.FINISH,
    }
)


def _evaluation_is_current(
    state: AgentState,
    key: str,
    input_fingerprint: str,
    legacy_matches: bool,
) -> bool:
    """优先校验 v13 完整输入指纹，旧检查点才回退到原行程指纹。"""

    if key in state.evaluation_input_fingerprints:
        return state.evaluation_input_fingerprints[key] == input_fingerprint
    return legacy_matches


class TripOrchestrator:
    """旅行规划总编排器：负责决策顺序，ExecutionPolicy 负责安全执行。"""

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        attraction_agent: Any = None,
        weather_agent: Any = None,
        hotel_agent: Any = None,
        planner_agent: Any = None,
        validator: TripPlanValidator | None = None,
        max_steps: int = 24,
        max_attempts_per_action: int = 2,
        max_repair_attempts: int = 2,
        max_route_optimization_attempts: int = 1,
        route_optimization_max_candidates: int = 6,
        route_optimization_min_improvement_percent: float = 10.0,
        route_optimizer: DeterministicRouteOptimizer | None = None,
        max_schedule_optimization_attempts: int = 1,
        schedule_optimization_max_candidates: int = 6,
        schedule_optimization_min_improvement_percent: float = 10.0,
        schedule_default_start_time: str = "09:00",
        schedule_default_end_time: str = "18:00",
        schedule_lunch_duration_minutes: int = 60,
        schedule_lunch_window_start: str = "11:30",
        schedule_lunch_window_end: str = "14:00",
        schedule_route_buffer_minutes: int = 10,
        schedule_attraction_buffer_minutes: int = 10,
        schedule_evaluator: ScheduleTimelineEvaluator | None = None,
        schedule_optimizer: DeterministicScheduleOptimizer | None = None,
        max_constraint_optimization_attempts: int = 1,
        constraint_optimization_max_candidates: int = 8,
        constraint_optimization_min_improvement_percent: float = 10.0,
        constraint_lunch_window_start: str = "11:30",
        constraint_lunch_window_end: str = "14:00",
        constraint_daily_attraction_soft_limit: int = 5,
        constraint_evaluator: ConstraintEvaluator | None = None,
        constraint_optimizer: DeterministicConstraintOptimizer | None = None,
        max_commute_replacement_attempts: int = 2,
        commute_replacement_max_candidates: int = 24,
        max_commute_supplement_searches: int = 2,
        commute_supplement_initial_radius_meters: int = 5000,
        commute_supplement_max_radius_meters: int = 20000,
        commute_supplement_page_size: int = 20,
        commute_supplement_pool_max_candidates: int = 48,
        commute_max_walking_minutes: int = 45,
        commute_max_transit_minutes: int = 90,
        commute_max_driving_minutes: int = 120,
        commute_evaluator: CommuteConstraintEvaluator | None = None,
        commute_optimizer: RemoteAttractionReplacementOptimizer | None = None,
        minimum_total_attractions: int = 0,
        max_content_refill_attempts: int = 2,
        content_refill_max_candidates: int = 24,
        content_refill_default_visit_duration_minutes: int = 120,
        content_refill_optimizer: MinimumAttractionRefillOptimizer | None = None,
        plan_consistency_rebuilder: TripPlanConsistencyRebuilder | None = None,
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
        partial_acceptance_allowed_error_codes: tuple[str, ...] = (
            DEFAULT_ALLOWED_PARTIAL_ERROR_CODES
        ),
        partial_acceptance_policy: PartialAcceptancePolicy | None = None,
        max_duration_seconds: float = 600.0,
        max_tool_calls: int = 30,
        max_llm_calls: int = 6,
        retry_base_delay_seconds: float = 0.5,
        retry_max_delay_seconds: float = 8.0,
        retry_jitter_seconds: float = 0.25,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout_seconds: float = 30.0,
        execution_policy: ExecutionPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        state_store: AgentStateStore | None = None,
        checkpoint_policy: CheckpointPolicy | None = None,
        checkpoint_max_attempts: int = 3,
        checkpoint_retry_base_delay_seconds: float = 0.05,
        checkpoint_retry_max_delay_seconds: float = 0.5,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_attempts_per_action < 1:
            raise ValueError("max_attempts_per_action must be at least 1")
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts cannot be negative")
        if max_route_optimization_attempts < 0:
            raise ValueError("max_route_optimization_attempts cannot be negative")
        if route_optimization_max_candidates < 1:
            raise ValueError("route_optimization_max_candidates must be at least 1")
        if route_optimization_min_improvement_percent < 0:
            raise ValueError(
                "route_optimization_min_improvement_percent cannot be negative"
            )
        if max_schedule_optimization_attempts < 0:
            raise ValueError("max_schedule_optimization_attempts cannot be negative")
        if schedule_optimization_max_candidates < 1:
            raise ValueError("schedule_optimization_max_candidates must be at least 1")
        if schedule_optimization_min_improvement_percent < 0:
            raise ValueError(
                "schedule_optimization_min_improvement_percent cannot be negative"
            )
        if max_constraint_optimization_attempts < 0:
            raise ValueError("max_constraint_optimization_attempts cannot be negative")
        if constraint_optimization_max_candidates < 1:
            raise ValueError("constraint_optimization_max_candidates must be at least 1")
        if constraint_optimization_min_improvement_percent < 0:
            raise ValueError(
                "constraint_optimization_min_improvement_percent cannot be negative"
            )
        if max_commute_replacement_attempts < 0:
            raise ValueError("max_commute_replacement_attempts cannot be negative")
        if commute_replacement_max_candidates < 1:
            raise ValueError("commute_replacement_max_candidates must be at least 1")
        if max_commute_supplement_searches < 0:
            raise ValueError("max_commute_supplement_searches cannot be negative")
        if minimum_total_attractions < 0:
            raise ValueError("minimum_total_attractions cannot be negative")
        if max_content_refill_attempts < 0:
            raise ValueError("max_content_refill_attempts cannot be negative")
        if content_refill_max_candidates < 1:
            raise ValueError("content_refill_max_candidates must be at least 1")
        if content_refill_default_visit_duration_minutes < 1:
            raise ValueError(
                "content_refill_default_visit_duration_minutes must be at least 1"
            )
        if max_repeated_action_inputs < 1:
            raise ValueError("max_repeated_action_inputs must be at least 1")
        if max_no_progress_steps < 1:
            raise ValueError("max_no_progress_steps must be at least 1")
        if max_local_actions_per_step < 1:
            raise ValueError("max_local_actions_per_step must be at least 1")
        if not 0 <= partial_acceptance_min_score <= 100:
            raise ValueError("partial_acceptance_min_score must be between 0 and 100")
        partial_limits = (
            partial_acceptance_max_validation_errors,
            partial_acceptance_max_schedule_overtime_minutes,
            partial_acceptance_max_unavailable_route_legs,
            partial_acceptance_max_excessive_commute_segments,
            partial_acceptance_max_constraint_errors,
            partial_acceptance_min_attractions_per_day,
        )
        if any(value < 0 for value in partial_limits):
            raise ValueError("partial acceptance limits cannot be negative")
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if max_tool_calls < 0 or max_llm_calls < 0:
            raise ValueError("call budgets cannot be negative")
        if checkpoint_max_attempts < 1:
            raise ValueError("checkpoint_max_attempts must be at least 1")

        if tool_registry is None:
            if planner_agent is None:
                raise ValueError("tool_registry or planner_agent is required")
            tool_registry = build_trip_tool_registry(
                planner_agent=planner_agent,
                attraction_agent=attraction_agent,
                weather_agent=weather_agent,
                hotel_agent=hotel_agent,
            )

        if execution_policy is not None and execution_policy.registry is not tool_registry:
            raise ValueError("execution_policy must use the orchestrator tool_registry")

        self.tool_registry = tool_registry
        self.validator = validator or TripPlanValidator(
            minimum_total_attractions=minimum_total_attractions
        )
        self.max_steps = max_steps
        self.max_attempts_per_action = max_attempts_per_action
        self.max_repair_attempts = max_repair_attempts
        self.max_route_optimization_attempts = max_route_optimization_attempts
        self.route_optimization_min_improvement_percent = (
            route_optimization_min_improvement_percent
        )
        self.route_optimizer = route_optimizer or DeterministicRouteOptimizer(
            max_candidates=route_optimization_max_candidates
        )
        self.max_schedule_optimization_attempts = (
            max_schedule_optimization_attempts
        )
        self.schedule_optimization_min_improvement_percent = (
            schedule_optimization_min_improvement_percent
        )
        self.schedule_evaluator = schedule_evaluator or ScheduleTimelineEvaluator(
            default_start_time=schedule_default_start_time,
            default_end_time=schedule_default_end_time,
            lunch_duration_minutes=schedule_lunch_duration_minutes,
            lunch_window_start=schedule_lunch_window_start,
            lunch_window_end=schedule_lunch_window_end,
            route_buffer_minutes=schedule_route_buffer_minutes,
            attraction_buffer_minutes=schedule_attraction_buffer_minutes,
        )
        self.schedule_optimizer = schedule_optimizer or DeterministicScheduleOptimizer(
            evaluator=self.schedule_evaluator,
            max_candidates=schedule_optimization_max_candidates,
            min_move_improvement_percent=(
                schedule_optimization_min_improvement_percent
            ),
        )
        self.max_constraint_optimization_attempts = (
            max_constraint_optimization_attempts
        )
        self.constraint_optimization_min_improvement_percent = (
            constraint_optimization_min_improvement_percent
        )
        self.constraint_evaluator = constraint_evaluator or ConstraintEvaluator(
            lunch_window_start=constraint_lunch_window_start,
            lunch_window_end=constraint_lunch_window_end,
            daily_attraction_soft_limit=constraint_daily_attraction_soft_limit,
        )
        self.constraint_optimizer = constraint_optimizer or DeterministicConstraintOptimizer(
            evaluator=self.constraint_evaluator,
            schedule_evaluator=self.schedule_evaluator,
            max_candidates=constraint_optimization_max_candidates,
        )
        self.max_commute_replacement_attempts = max_commute_replacement_attempts
        self.max_commute_supplement_searches = max_commute_supplement_searches
        self.commute_supplementer = CommuteCandidatePoolSupplementer(
            initial_radius_meters=commute_supplement_initial_radius_meters,
            max_radius_meters=commute_supplement_max_radius_meters,
            page_size=commute_supplement_page_size,
            pool_max_candidates=commute_supplement_pool_max_candidates,
        )
        self.commute_evaluator = commute_evaluator or CommuteConstraintEvaluator(
            max_walking_minutes=commute_max_walking_minutes,
            max_transit_minutes=commute_max_transit_minutes,
            max_driving_minutes=commute_max_driving_minutes,
        )
        self.commute_optimizer = (
            commute_optimizer
            or RemoteAttractionReplacementOptimizer(
                max_candidates=commute_replacement_max_candidates,
                default_visit_duration_minutes=(
                    content_refill_default_visit_duration_minutes
                ),
            )
        )
        self.minimum_total_attractions = minimum_total_attractions
        self.max_content_refill_attempts = max_content_refill_attempts
        self.content_refill_optimizer = (
            content_refill_optimizer
            or MinimumAttractionRefillOptimizer(
                evaluator=self.schedule_evaluator,
                minimum_total_attractions=max(1, minimum_total_attractions),
                max_candidates=content_refill_max_candidates,
                default_visit_duration_minutes=(
                    content_refill_default_visit_duration_minutes
                ),
            )
        )
        self.plan_consistency_rebuilder = (
            plan_consistency_rebuilder
            or TripPlanConsistencyRebuilder(
                lunch_window_start=constraint_lunch_window_start,
                lunch_window_end=constraint_lunch_window_end,
            )
        )
        self.max_repeated_action_inputs = max_repeated_action_inputs
        self.max_no_progress_steps = max_no_progress_steps
        self.max_local_actions_per_step = max_local_actions_per_step
        self.partial_acceptance_enabled = partial_acceptance_enabled
        self.partial_acceptance_min_score = partial_acceptance_min_score
        self.partial_acceptance_max_validation_errors = (
            partial_acceptance_max_validation_errors
        )
        self.partial_acceptance_max_schedule_overtime_minutes = (
            partial_acceptance_max_schedule_overtime_minutes
        )
        self.partial_acceptance_max_unavailable_route_legs = (
            partial_acceptance_max_unavailable_route_legs
        )
        self.partial_acceptance_max_excessive_commute_segments = (
            partial_acceptance_max_excessive_commute_segments
        )
        self.partial_acceptance_max_constraint_errors = (
            partial_acceptance_max_constraint_errors
        )
        self.partial_acceptance_min_attractions_per_day = (
            partial_acceptance_min_attractions_per_day
        )
        self.partial_acceptance_allowed_error_codes = list(
            partial_acceptance_allowed_error_codes
        )
        self.partial_acceptance_policy = (
            partial_acceptance_policy or PartialAcceptancePolicy()
        )
        self.max_duration_seconds = max_duration_seconds
        self.max_tool_calls = max_tool_calls
        self.max_llm_calls = max_llm_calls
        self.state_store = state_store
        # 检查点使用独立的有限重试策略，避免 SQLite 短暂锁竞争中断整个会话。
        self.checkpoint_policy = checkpoint_policy or CheckpointPolicy(
            max_attempts=checkpoint_max_attempts,
            base_delay_seconds=checkpoint_retry_base_delay_seconds,
            max_delay_seconds=checkpoint_retry_max_delay_seconds,
        )
        self.execution_policy = execution_policy or ExecutionPolicy(
            tool_registry,
            retry_base_delay_seconds=retry_base_delay_seconds,
            retry_max_delay_seconds=retry_max_delay_seconds,
            retry_jitter_seconds=retry_jitter_seconds,
            circuit_breaker=circuit_breaker
            or CircuitBreaker(
                failure_threshold=circuit_failure_threshold,
                recovery_timeout_seconds=circuit_recovery_timeout_seconds,
            ),
        )

    def run(
        self,
        request: TripRequest,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> AgentState:
        """创建一个新会话、持久化初始状态并执行确定性循环。"""

        # 步骤 1：为新请求创建独立状态，并把执行上限固化到会话预算中。
        state = AgentState.create(
            request,
            max_steps=self.max_steps,
            max_repair_attempts=self.max_repair_attempts,
            max_route_optimization_attempts=self.max_route_optimization_attempts,
            max_schedule_optimization_attempts=(
                self.max_schedule_optimization_attempts
            ),
            max_constraint_optimization_attempts=(
                self.max_constraint_optimization_attempts
            ),
            max_commute_replacement_attempts=self.max_commute_replacement_attempts,
            max_commute_supplement_searches=self.max_commute_supplement_searches,
            max_content_refill_attempts=self.max_content_refill_attempts,
            minimum_total_attractions=self.minimum_total_attractions,
            max_repeated_action_inputs=self.max_repeated_action_inputs,
            max_no_progress_steps=self.max_no_progress_steps,
            max_local_actions_per_step=self.max_local_actions_per_step,
            partial_acceptance_enabled=self.partial_acceptance_enabled,
            partial_acceptance_min_score=self.partial_acceptance_min_score,
            partial_acceptance_max_validation_errors=(
                self.partial_acceptance_max_validation_errors
            ),
            partial_acceptance_max_schedule_overtime_minutes=(
                self.partial_acceptance_max_schedule_overtime_minutes
            ),
            partial_acceptance_max_unavailable_route_legs=(
                self.partial_acceptance_max_unavailable_route_legs
            ),
            partial_acceptance_max_excessive_commute_segments=(
                self.partial_acceptance_max_excessive_commute_segments
            ),
            partial_acceptance_max_constraint_errors=(
                self.partial_acceptance_max_constraint_errors
            ),
            partial_acceptance_min_attractions_per_day=(
                self.partial_acceptance_min_attractions_per_day
            ),
            partial_acceptance_allowed_error_codes=(
                self.partial_acceptance_allowed_error_codes
            ),
            max_duration_seconds=self.max_duration_seconds,
            max_tool_calls=self.max_tool_calls,
            max_llm_calls=self.max_llm_calls,
            session_id=session_id,
            user_id=user_id,
        )
        # 步骤 2：进入确定性循环；循环中的每个动作都会写入检查点。
        return self._run_state(state)

    def resume(self, state: AgentState) -> AgentState:
        """从检查点继续执行，同时保留所有会话生命周期预算。"""

        # 已完成会话保持幂等，重复恢复不会再次调用高德或 LLM。
        if state.finished or state.status == "completed":
            return state
        # 未完成会话从已有数据继续，预算和历史计数不会被重置。
        return self._run_state(state)

    def _run_state(self, state: AgentState) -> AgentState:
        # 步骤 1：标记会话正在执行，并在任何外部调用前保存初始检查点。
        state.status = "running"
        self._checkpoint(state)
        # 这里只统计“本次 run/resume”的尝试次数，终身次数保存在 AgentState 中。
        attempts_in_run: dict[str, int] = {}

        # 步骤 2：每轮执行一个物理动作，再尽可能吸收后续本地状态跳转。
        while not state.finished and state.current_step < state.max_steps:
            # 异步 Worker 通过 ContextVar 注入取消检查；同步接口调用时是空操作。
            raise_if_task_cancelled()
            runtime_reason = self.execution_policy.runtime_budget_reason(state)
            if runtime_reason:
                self._raise_budget_exhausted(state, runtime_reason)

            action = self.decide_next_action(state)
            notify_action_started(state, action.value)
            input_fingerprint, success_key = self._prepare_convergence_action(
                state, action
            )
            state_fingerprint_before = business_state_fingerprint(state)
            history_length = len(state.action_history)
            key = action.value
            attempts_in_run[key] = attempts_in_run.get(key, 0) + 1
            try:
                self.execute_action(
                    state,
                    action,
                    attempt_in_run=attempts_in_run[key],
                )
                successful_record = self._find_successful_action_record(
                    state, action, history_length
                )
                if successful_record is not None:
                    self._record_convergence_result(
                        state,
                        action=action,
                        input_fingerprint=input_fingerprint,
                        success_key=success_key,
                        state_fingerprint_before=state_fingerprint_before,
                        action_record=successful_record,
                    )
                    self._drain_local_actions(
                        state,
                        batch_root_action=action,
                        root_record=successful_record,
                    )
            finally:
                # 写检查点前先确认租约，避免旧 Worker 在恢复任务后覆盖新 Worker 状态。
                raise_if_task_lease_lost()
                # 一个物理步骤只写入一次常规检查点；子动作已全部进入 action_history。
                self._checkpoint(state)
                notify_action_completed(state, action.value)

        # 步骤 3：达到最大物理步数仍未结束时，明确失败。
        if not state.finished:
            state.status = "max_steps_reached"
            state.budget_exhausted_reason = (
                f"智能体达到最大执行步数 {state.max_steps}"
            )
            message = f"{state.budget_exhausted_reason}，尚未生成完整结果"
            state.errors.append(message)
            self._checkpoint(state)
            raise AgentMaxStepsError(message, state)

        return state

    def _prepare_convergence_action(
        self, state: AgentState, action: AgentAction
    ) -> tuple[str, str]:
        """生成动作输入指纹，并在真正执行前拦截重复成功动作。"""

        input_fingerprint = action_input_fingerprint(state, action)
        success_key = f"{action.value}:{input_fingerprint}"
        successful_count = state.successful_action_inputs.get(success_key, 0)
        if successful_count >= state.execution_budget.max_repeated_action_inputs:
            self._raise_convergence_stopped(
                state,
                f"动作 {action.value} 在相同业务输入下已成功执行 "
                f"{successful_count} 次，拒绝重复执行",
            )
        return input_fingerprint, success_key

    @staticmethod
    def _find_successful_action_record(
        state: AgentState,
        action: AgentAction,
        history_length: int,
    ) -> ActionRecord | None:
        """从本次动作新增的审计记录中找到成功项。"""

        return next(
            (
                record
                for record in reversed(state.action_history[history_length:])
                if record.action is action and record.success
            ),
            None,
        )

    def _drain_local_actions(
        self,
        state: AgentState,
        *,
        batch_root_action: AgentAction,
        root_record: ActionRecord,
    ) -> None:
        """在当前物理步骤内连续执行可安全压缩的确定性本地动作。"""

        # 自定义编排器如果重写 execute_action，默认保留其原有的一步一动作语义。
        if type(self).execute_action is not TripOrchestrator.execute_action:
            return

        batch_started = False
        for batch_index in range(1, state.execution_budget.max_local_actions_per_step + 1):
            raise_if_task_cancelled()
            if state.finished:
                break
            runtime_reason = self.execution_policy.runtime_budget_reason(state)
            if runtime_reason:
                self._raise_budget_exhausted(state, runtime_reason)

            action = self.decide_next_action(state)
            if action not in _COMPRESSIBLE_LOCAL_ACTIONS:
                break

            if not batch_started:
                state.local_action_batch_count += 1
                batch_started = True

            input_fingerprint, success_key = self._prepare_convergence_action(
                state, action
            )
            state_fingerprint_before = business_state_fingerprint(state)
            history_length = len(state.action_history)
            try:
                self.execute_action(state, action, advance_step=False)
            finally:
                # 即使本地动作以异常结束，也要给已经写入的失败记录补齐批次元数据。
                new_records = state.action_history[history_length:]
                for record in new_records:
                    record.compressed = True
                    record.batch_root_action = batch_root_action
                    record.batch_index = batch_index
                if new_records:
                    root_record.compressed_actions.append(action)
                    state.compressed_local_action_count += 1

            successful_record = self._find_successful_action_record(
                state, action, history_length
            )
            if successful_record is None:
                # 例如最终校验失败时，立即把控制权交回外层以决定是否调用 LLM 修复。
                break

            self._record_convergence_result(
                state,
                action=action,
                input_fingerprint=input_fingerprint,
                success_key=success_key,
                state_fingerprint_before=state_fingerprint_before,
                action_record=successful_record,
            )

    def _record_convergence_result(
        self,
        state: AgentState,
        *,
        action: AgentAction,
        input_fingerprint: str,
        success_key: str,
        state_fingerprint_before: str,
        action_record: ActionRecord,
    ) -> None:
        """成功动作完成后保存业务进展证据。"""

        state_fingerprint_after = business_state_fingerprint(state)
        made_progress = state_fingerprint_before != state_fingerprint_after
        action_record.input_fingerprint = input_fingerprint
        action_record.state_fingerprint_before = state_fingerprint_before
        action_record.state_fingerprint_after = state_fingerprint_after
        action_record.made_progress = made_progress
        if made_progress:
            # 业务状态发生变化后开启新的收敛窗口，只保留
            # 产生新状态的当前动作，使后续回滚或恢复
            # 可以合法地再次执行曾经出现过的输入。
            state.successful_action_inputs.clear()
            state.successful_action_inputs[success_key] = 1
        else:
            state.successful_action_inputs[success_key] = (
                state.successful_action_inputs.get(success_key, 0) + 1
            )
        state.convergence_history.append(
            ConvergenceRecord(
                step=state.current_step,
                action=action,
                input_fingerprint=input_fingerprint,
                state_fingerprint_before=state_fingerprint_before,
                state_fingerprint_after=state_fingerprint_after,
                made_progress=made_progress,
                reason=(
                    "业务状态已变化"
                    if made_progress
                    else "业务状态未变化"
                ),
            )
        )
        if made_progress:
            state.no_progress_streak = 0
            return

        state.no_progress_streak += 1
        state.no_progress_total += 1
        if state.no_progress_streak >= state.execution_budget.max_no_progress_steps:
            self._raise_convergence_stopped(
                state,
                f"连续 {state.no_progress_streak} 个成功动作未改变业务状态",
            )

    def _raise_convergence_stopped(self, state: AgentState, reason: str) -> None:
        """持久化收敛终止状态，并停止浪费后续执行预算。"""

        state.status = "convergence_stopped"
        state.convergence_terminated_reason = reason
        if reason not in state.errors:
            state.errors.append(reason)
        self._checkpoint(state)
        raise AgentConvergenceError(reason, state)

    @staticmethod
    def decide_next_action(state: AgentState) -> AgentAction:
        """仅根据 AgentState 确定下一动作，决策过程不调用 LLM。"""

        # 先按顺序补齐三类外部事实数据。
        if state.attractions is None:
            return AgentAction.SEARCH_ATTRACTIONS
        if state.weather is None:
            return AgentAction.GET_WEATHER
        if state.hotels is None:
            return AgentAction.SEARCH_HOTELS
        # 基础数据齐全后，才允许 LLM 生成结构化行程。
        if state.trip_plan is None:
            return AgentAction.GENERATE_PLAN
        current_route_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        route_quality_fingerprint = route_quality_input_fingerprint(
            state.request, state.trip_plan, state.route_estimates
        )
        schedule_fingerprint = schedule_input_fingerprint(
            state.request, state.trip_plan, state.route_estimates
        )
        commute_fingerprint = commute_input_fingerprint(
            state.request, state.trip_plan, state.route_estimates
        )
        if (
            state.route_estimates is None
            or state.route_plan_fingerprint != current_route_fingerprint
        ):
            return AgentAction.ESTIMATE_ROUTES

        # 已经生成的周边补搜请求应优先执行，避免被其他评估阶段阻塞。
        if state.content_refill_status == "supplement_needed":
            return AgentAction.SUPPLEMENT_ATTRACTIONS

        # 当前路线快照必须先完成质量评分，才能进入后续时间轴评估；
        # 该步骤也用于自动补齐旧版 SQLite 检查点缺失的路线评分。
        if (
            state.route_quality_report is None
            or not _evaluation_is_current(
                state,
                "route_quality",
                route_quality_fingerprint,
                state.route_quality_plan_fingerprint == current_route_fingerprint,
            )
        ):
            return AgentAction.OPTIMIZE_ROUTES

        # 路线排序优化必须先达到完成或跳过状态，才能进行跨日日程平衡。
        if state.route_optimization_status == "candidate_pending":
            return AgentAction.OPTIMIZE_ROUTES
        if (
            state.route_optimization_status == "not_started"
            and state.route_quality_report.optimization_recommended
            and state.route_optimization_count
            < state.execution_budget.max_route_optimization_attempts
        ):
            return AgentAction.OPTIMIZE_ROUTES

        # 先完成旧优化阶段遗留候选的真实路线复验，
        # 再开始通勤替换，避免不同优化器的回滚基线发生嵌套。
        schedule_current = (
            state.schedule_quality_report is not None
            and _evaluation_is_current(
                state,
                "schedule",
                schedule_fingerprint,
                state.schedule_quality_plan_fingerprint == current_route_fingerprint,
            )
        )
        if state.schedule_optimization_status == "candidate_pending":
            return (
                AgentAction.OPTIMIZE_SCHEDULE
                if schedule_current
                else AgentAction.EVALUATE_SCHEDULE
            )
        if state.constraint_optimization_status == "candidate_pending":
            if not schedule_current:
                return AgentAction.EVALUATE_SCHEDULE
            current_constraint_fingerprint = constraint_plan_fingerprint(
                state.request,
                state.trip_plan,
            )
            current_constraint_input = constraint_input_fingerprint(
                state.request,
                state.trip_plan,
                state.schedule_quality_report,
                state.attractions,
                state.weather,
            )
            if (
                state.constraint_report is None
                or not _evaluation_is_current(
                    state,
                    "constraints",
                    current_constraint_input,
                    state.constraint_plan_fingerprint == current_constraint_fingerprint,
                )
            ):
                return AgentAction.EVALUATE_CONSTRAINTS
            return AgentAction.OPTIMIZE_CONSTRAINTS
        if state.content_refill_status == "candidate_pending":
            if not schedule_current:
                return AgentAction.EVALUATE_SCHEDULE
            current_constraint_fingerprint = constraint_plan_fingerprint(
                state.request,
                state.trip_plan,
            )
            current_constraint_input = constraint_input_fingerprint(
                state.request,
                state.trip_plan,
                state.schedule_quality_report,
                state.attractions,
                state.weather,
            )
            if (
                state.constraint_report is None
                or not _evaluation_is_current(
                    state,
                    "constraints",
                    current_constraint_input,
                    state.constraint_plan_fingerprint == current_constraint_fingerprint,
                )
            ):
                return AgentAction.EVALUATE_CONSTRAINTS
            return AgentAction.REFILL_ATTRACTIONS

        # 后续优化器可能移动或删除过远景点，因此必须先按交通方式评估
        # 单段通勤上限，保留问题根因。
        if (
            state.commute_report is None
            or not _evaluation_is_current(
                state,
                "commute",
                commute_fingerprint,
                state.commute_plan_fingerprint == current_route_fingerprint,
            )
        ):
            return AgentAction.EVALUATE_COMMUTE

        # 对旧检查点或已经过期的派生数据，在本地重新构建完整时间轴。
        if not schedule_current:
            return AgentAction.EVALUATE_SCHEDULE

        # 候选池补充作为显式工具动作执行，使 HTTP 超时、
        # 指数退避、执行预算和熔断器仍由统一策略管理。
        if state.commute_optimization_status == "supplement_needed":
            return AgentAction.SUPPLEMENT_ATTRACTIONS

        # 过远景点替换候选必须使用新的高德真实路线、
        # 完整时间轴和可执行性约束复验后才能接受。
        commute_needs_action = (
            state.commute_optimization_status == "candidate_pending"
            or (
                state.commute_optimization_status == "not_started"
                and state.commute_report.optimization_recommended
            )
        )
        if commute_needs_action:
            current_constraint_fingerprint = constraint_plan_fingerprint(
                state.request,
                state.trip_plan,
            )
            current_constraint_input = constraint_input_fingerprint(
                state.request,
                state.trip_plan,
                state.schedule_quality_report,
                state.attractions,
                state.weather,
            )
            if (
                state.constraint_report is None
                or not _evaluation_is_current(
                    state,
                    "constraints",
                    current_constraint_input,
                    state.constraint_plan_fingerprint == current_constraint_fingerprint,
                )
            ):
                return AgentAction.EVALUATE_CONSTRAINTS
            return AgentAction.REPLACE_REMOTE_ATTRACTION

        # 跨日移动后的景点必须重新查询真实路线，不能直接接受近似结果。
        if (
            state.schedule_optimization_status == "not_started"
            and state.schedule_quality_report.optimization_recommended
        ):
            return AgentAction.OPTIMIZE_SCHEDULE

        required_attractions = min(
            state.execution_budget.minimum_total_attractions,
            max(1, state.request.travel_days),
        )
        refill_needed = (
            count_attractions(state.trip_plan) < required_attractions
            and state.content_refill_status not in {"completed", "skipped"}
            and state.content_refill_count
            < state.execution_budget.max_content_refill_attempts
        )

        # 景点回填候选及其基线都必须先完成当前可执行性约束评估；
        # 景点结构稳定后再重建描述和费用，
        # 并在最终约束检查前生成派生用餐和预算数据，
        # 从而避免无意义的重复评估。
        constraints_required_for_refill = (
            state.content_refill_status == "candidate_pending"
            or refill_needed
            or state.constraint_optimization_status == "candidate_pending"
        )
        current_constraint_fingerprint = constraint_plan_fingerprint(
            state.request,
            state.trip_plan,
        )
        current_constraint_input = constraint_input_fingerprint(
            state.request,
            state.trip_plan,
            state.schedule_quality_report,
            state.attractions,
            state.weather,
        )
        constraints_current = (
            state.constraint_report is not None
            and _evaluation_is_current(
                state,
                "constraints",
                current_constraint_input,
                state.constraint_plan_fingerprint == current_constraint_fingerprint,
            )
        )
        if constraints_required_for_refill:
            if not constraints_current:
                return AgentAction.EVALUATE_CONSTRAINTS
            if state.constraint_optimization_status == "candidate_pending":
                return AgentAction.OPTIMIZE_CONSTRAINTS
            if (
                state.constraint_optimization_status == "not_started"
                and state.constraint_report.optimization_recommended
            ):
                return AgentAction.OPTIMIZE_CONSTRAINTS
            return AgentAction.REFILL_ATTRACTIONS

        # 路线、时间轴、约束和景点回填稳定后，再围绕最终地点搜索真实餐厅。
        # 这样不会因前序优化改变景点顺序而重复消耗高德调用。
        restaurant_fingerprint = restaurant_search_source_fingerprint(
            state.trip_plan,
            max_anchors=settings.AMAP_MAX_RESTAURANT_SEARCH_ANCHORS,
        )
        if (
            state.restaurants is None
            or state.restaurant_plan_fingerprint != restaurant_fingerprint
        ):
            return AgentAction.SEARCH_RESTAURANTS

        content_fingerprint = plan_content_source_fingerprint(
            state.request,
            state.trip_plan,
            state.route_estimates,
            state.schedule_quality_report,
            state.restaurants,
        )
        if state.plan_consistency_fingerprint != content_fingerprint:
            return AgentAction.REBUILD_PLAN_CONTENT

        # 重建描述、餐饮、交通文案和预算会改变约束输入，
        # 因此完成内容重建后必须刷新最终约束报告。
        current_constraint_fingerprint = constraint_plan_fingerprint(
            state.request,
            state.trip_plan,
        )
        current_constraint_input = constraint_input_fingerprint(
            state.request,
            state.trip_plan,
            state.schedule_quality_report,
            state.attractions,
            state.weather,
        )
        if (
            state.constraint_report is None
            or not _evaluation_is_current(
                state,
                "constraints",
                current_constraint_input,
                state.constraint_plan_fingerprint == current_constraint_fingerprint,
            )
        ):
            return AgentAction.EVALUATE_CONSTRAINTS
        if state.constraint_optimization_status == "candidate_pending":
            return AgentAction.OPTIMIZE_CONSTRAINTS
        if (
            state.constraint_optimization_status == "not_started"
            and state.constraint_report.optimization_recommended
        ):
            return AgentAction.OPTIMIZE_CONSTRAINTS

        current_validation_input = validation_input_fingerprint(
            state.request,
            state.trip_plan,
            state.attractions,
            state.weather,
            state.hotels,
            state.route_estimates,
            state.schedule_quality_report,
            state.constraint_report,
        )
        if (
            state.last_validation_result is None
            or not _evaluation_is_current(
                state,
                "validation",
                current_validation_input,
                state.last_validation_result is not None,
            )
        ):
            return AgentAction.VALIDATE_PLAN
        # 完整校验通过，或已达到部分可接受标准时，都可以进入终态。
        # 后者会在 FINISH 中记录 partial 完成模式和未解决警告。
        if (
            state.last_validation_result.valid
            or (
                state.acceptance_report is not None
                and state.acceptance_report.accepted
            )
        ):
            return AgentAction.FINISH
        # 仅当问题可修复且修复预算尚未耗尽时，才再次调用 LLM。
        if (
            state.last_validation_result.repairable
            and state.repair_count < state.max_repair_attempts
        ):
            return AgentAction.REPAIR_PLAN
        return AgentAction.VALIDATE_PLAN

    def execute_action(
        self,
        state: AgentState,
        action: AgentAction,
        *,
        attempt_in_run: int | None = None,
        advance_step: bool = True,
    ) -> None:
        """执行一个动作；压缩子动作可共用当前物理步骤。"""

        # 只有物理根动作递增 current_step，被压缩的本地子动作共用步骤号。
        if advance_step:
            state.current_step += 1
        elif state.current_step < 1:
            raise ValueError("compressed action requires an active physical step")
        lifetime_attempt = state.next_attempt(action)
        current_run_attempt = attempt_in_run or lifetime_attempt
        reason = _ACTION_REASONS[action]

        # 步骤 2：FINISH 是纯状态变更，不调用外部工具。
        if action is AgentAction.FINISH:
            state.finished = True
            state.status = "completed"
            partial = bool(
                state.acceptance_report is not None
                and state.acceptance_report.accepted
                and state.acceptance_report.partial
            )
            state.completion_mode = "partial" if partial else "full"
            state.completion_warnings = (
                list(state.acceptance_report.warnings)
                if state.acceptance_report is not None
                else []
            )
            if partial:
                reason = "行程达到最低可接受标准，保留非关键问题后结束执行"
            state.action_history.append(
                ActionRecord(
                    step=state.current_step,
                    action=action,
                    reason=reason,
                    attempt=lifetime_attempt,
                    success=True,
                )
            )
            return

        # 步骤 3：VALIDATE_PLAN 使用本地规则校验，也不调用 LLM。
        if action is AgentAction.VALIDATE_PLAN:
            self._validate_plan(state, reason, lifetime_attempt)
            return

        # 步骤 4：其余动作通过 ExecutionPolicy 调用白名单工具。
        # 本地确定性评估和优化不消耗工具调用预算，也不消耗 LLM 预算。
        if action is AgentAction.OPTIMIZE_ROUTES:
            self._optimize_routes(state, reason, lifetime_attempt)
            return
        if action is AgentAction.EVALUATE_COMMUTE:
            self._evaluate_commute(state, reason, lifetime_attempt)
            return
        if action is AgentAction.REPLACE_REMOTE_ATTRACTION:
            self._replace_remote_attraction(state, reason, lifetime_attempt)
            return
        if action is AgentAction.EVALUATE_SCHEDULE:
            self._evaluate_schedule(state, reason, lifetime_attempt)
            return
        if action is AgentAction.OPTIMIZE_SCHEDULE:
            self._optimize_schedule(state, reason, lifetime_attempt)
            return
        if action is AgentAction.EVALUATE_CONSTRAINTS:
            self._evaluate_constraints(state, reason, lifetime_attempt)
            return
        if action is AgentAction.OPTIMIZE_CONSTRAINTS:
            self._optimize_constraints(state, reason, lifetime_attempt)
            return
        if action is AgentAction.REFILL_ATTRACTIONS:
            self._refill_attractions(state, reason, lifetime_attempt)
            return
        if action is AgentAction.REBUILD_PLAN_CONTENT:
            self._rebuild_plan_content(state, reason, lifetime_attempt)
            return

        tool_result = self.execution_policy.execute_once(
            state,
            action.value,
            self._build_tool_payload(state, action),
        )
        state.last_action_result = tool_result

        # 步骤 5：统一处理工具失败、预算耗尽、指数退避和停止重试。
        if not tool_result.success:
            record = self._append_failed_action(
                state,
                action,
                reason,
                lifetime_attempt,
                tool_result,
            )
            if tool_result.error_type is ToolErrorType.BUDGET_EXCEEDED:
                self._raise_budget_exhausted(
                    state,
                    tool_result.error or "执行预算已耗尽",
                )

            decision = self.execution_policy.decide_retry(
                state,
                tool_result,
                attempt_in_run=current_run_attempt,
                max_attempts=self.max_attempts_per_action,
            )
            if decision.budget_reason:
                self._raise_budget_exhausted(state, decision.budget_reason)
            if decision.should_retry:
                delay_ms = max(0, round(decision.delay_seconds * 1000))
                record.retry_delay_ms = delay_ms
                state.total_retry_count += 1
                state.total_retry_delay_ms += delay_ms
                # 等待前先持久化失败记录和重试延迟，进程退出后仍可复盘。
                self._checkpoint(state)
                sleep_with_task_cancellation(decision.delay_seconds)
                return

            if action is AgentAction.SUPPLEMENT_ATTRACTIONS:
                self._handle_failed_attraction_supplement(
                    state,
                    tool_result.error or "Amap nearby candidate search failed",
                )
                return
            state.status = "failed"
            raise AgentActionError(
                action,
                tool_result.error or "工具执行失败",
                state,
                attempt=lifetime_attempt,
            )

        # 步骤 6：工具成功后，把标准化结果写入 AgentState。
        try:
            self._apply_tool_result(state, action, tool_result)
        except Exception as exc:
            invalid_result = ActionResult(
                tool_name=tool_result.tool_name,
                success=False,
                error=self._safe_error_message(exc),
                error_type=ToolErrorType.INVALID_OUTPUT,
                retryable=True,
                duration_ms=tool_result.duration_ms,
                circuit_state=tool_result.circuit_state,
            )
            state.last_action_result = invalid_result
            record = self._append_failed_action(
                state,
                action,
                reason,
                lifetime_attempt,
                invalid_result,
            )
            decision = self.execution_policy.decide_retry(
                state,
                invalid_result,
                attempt_in_run=current_run_attempt,
                max_attempts=self.max_attempts_per_action,
            )
            if decision.budget_reason:
                self._raise_budget_exhausted(state, decision.budget_reason)
            if decision.should_retry:
                delay_ms = max(0, round(decision.delay_seconds * 1000))
                record.retry_delay_ms = delay_ms
                state.total_retry_count += 1
                state.total_retry_delay_ms += delay_ms
                self._checkpoint(state)
                sleep_with_task_cancellation(decision.delay_seconds)
                return
            if action is AgentAction.SUPPLEMENT_ATTRACTIONS:
                self._handle_failed_attraction_supplement(
                    state,
                    invalid_result.error or "Amap nearby candidate search failed",
                )
                return
            state.status = "failed"
            raise AgentActionError(
                action,
                invalid_result.error or "工具输出无效",
                state,
                attempt=lifetime_attempt,
            )

        # 步骤 7：记录本次成功动作，供 SQLite 持久化和前端复盘。
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=action,
                reason=reason,
                attempt=lifetime_attempt,
                success=True,
                tool_name=tool_result.tool_name,
                duration_ms=tool_result.duration_ms,
                circuit_state=tool_result.circuit_state,
            )
        )

    @staticmethod
    def _refresh_route_quality(state: AgentState):
        """基于当前行程和路线快照生成路线质量报告。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for scoring")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        route_result = RouteEstimateResult.model_validate(state.route_estimates)
        if route_result.plan_fingerprint != current_fingerprint:
            raise ValueError("Route estimates do not match the current trip plan")
        report = evaluate_route_quality(state.trip_plan, route_result)
        state.route_quality_report = report
        state.route_quality_plan_fingerprint = current_fingerprint
        state.evaluation_input_fingerprints["route_quality"] = (
            route_quality_input_fingerprint(
                state.request, state.trip_plan, state.route_estimates
            )
        )
        return report

    @staticmethod
    def _invalidate_evaluation_fingerprints(
        state: AgentState, *keys: str
    ) -> None:
        """派生评估的业务输入改变后，清理对应的输入指纹。"""

        for key in keys:
            state.evaluation_input_fingerprints.pop(key, None)
        if "validation" in keys:
            state.acceptance_report = None
            state.completion_mode = None
            state.completion_warnings = []

    @staticmethod
    def _synchronize_evaluation_fingerprints(state: AgentState) -> None:
        """回滚到基线后，根据已恢复的评估结果重新同步业务输入指纹。"""

        for key in ("route_quality", "commute", "schedule", "constraints", "validation"):
            state.evaluation_input_fingerprints.pop(key, None)
        if state.trip_plan is None or state.route_estimates is None:
            return

        route_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        if (
            state.route_quality_report is not None
            and state.route_quality_plan_fingerprint == route_fingerprint
        ):
            state.evaluation_input_fingerprints["route_quality"] = (
                route_quality_input_fingerprint(
                    state.request, state.trip_plan, state.route_estimates
                )
            )
        if (
            state.commute_report is not None
            and state.commute_plan_fingerprint == route_fingerprint
        ):
            state.evaluation_input_fingerprints["commute"] = commute_input_fingerprint(
                state.request, state.trip_plan, state.route_estimates
            )
        if (
            state.schedule_quality_report is not None
            and state.schedule_quality_plan_fingerprint == route_fingerprint
        ):
            state.evaluation_input_fingerprints["schedule"] = schedule_input_fingerprint(
                state.request, state.trip_plan, state.route_estimates
            )
        if (
            state.constraint_report is not None
            and state.schedule_quality_report is not None
            and state.constraint_plan_fingerprint
            == constraint_plan_fingerprint(state.request, state.trip_plan)
        ):
            state.evaluation_input_fingerprints["constraints"] = (
                constraint_input_fingerprint(
                    state.request,
                    state.trip_plan,
                    state.schedule_quality_report,
                    state.attractions,
                    state.weather,
                )
            )

    @staticmethod
    def _clear_route_analysis(
        state: AgentState,
        *,
        reset_optimization_count: bool,
    ) -> None:
        """行程结构变化后清理所有由旧路线派生的数据。"""

        TripOrchestrator._invalidate_evaluation_fingerprints(
            state,
            "route_quality",
            "commute",
            "schedule",
            "constraints",
            "validation",
        )
        state.route_estimates = None
        state.route_plan_fingerprint = None
        state.route_quality_report = None
        state.route_quality_plan_fingerprint = None
        state.route_optimization_status = "not_started"
        state.route_optimization_candidate = None
        state.route_optimization_baseline_plan = None
        state.route_optimization_baseline_routes = None
        state.route_optimization_baseline_quality = None
        state.route_optimization_baseline_fingerprint = None
        TripOrchestrator._clear_commute_analysis(
            state,
            reset_optimization_count=reset_optimization_count,
        )
        state.schedule_quality_report = None
        state.schedule_quality_plan_fingerprint = None
        state.schedule_optimization_status = "not_started"
        state.schedule_optimization_candidate = None
        state.schedule_optimization_baseline_plan = None
        state.schedule_optimization_baseline_routes = None
        state.schedule_optimization_baseline_route_quality = None
        state.schedule_optimization_baseline_quality = None
        state.schedule_optimization_baseline_fingerprint = None
        TripOrchestrator._clear_constraint_analysis(
            state,
            reset_optimization_count=reset_optimization_count,
        )
        if reset_optimization_count:
            state.route_optimization_count = 0
            state.schedule_optimization_count = 0
        state.plan_consistency_fingerprint = None
        state.last_validation_result = None

    @staticmethod
    def _clear_constraint_analysis(
        state: AgentState,
        *,
        reset_optimization_count: bool,
    ) -> None:
        """清理所有由旧可执行性约束派生的报告和优化基线。"""

        TripOrchestrator._invalidate_evaluation_fingerprints(
            state, "constraints", "validation"
        )
        state.constraint_report = None
        state.constraint_plan_fingerprint = None
        state.constraint_optimization_status = "not_started"
        state.constraint_optimization_candidate = None
        state.constraint_optimization_baseline_plan = None
        state.constraint_optimization_baseline_routes = None
        state.constraint_optimization_baseline_route_quality = None
        state.constraint_optimization_baseline_schedule = None
        state.constraint_optimization_baseline_report = None
        state.constraint_optimization_baseline_fingerprint = None
        if reset_optimization_count:
            state.constraint_optimization_count = 0

    @staticmethod
    def _clear_route_optimization_baseline(state: AgentState) -> None:
        state.route_optimization_candidate = None
        state.route_optimization_baseline_plan = None
        state.route_optimization_baseline_routes = None
        state.route_optimization_baseline_quality = None
        state.route_optimization_baseline_fingerprint = None

    def _record_local_route_action(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=AgentAction.OPTIMIZE_ROUTES,
                reason=reason,
                attempt=lifetime_attempt,
                success=True,
            )
        )

    def _optimize_routes(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """生成或复验一个有界的路线顺序优化候选。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for optimization")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        report = state.route_quality_report
        if (
            report is None
            or state.route_quality_plan_fingerprint != current_fingerprint
        ):
            report = self._refresh_route_quality(state)

        # 第二轮使用候选的高德真实路线质量与已持久化基线比较；
        # 如果拒绝候选，则直接恢复完整基线数据，不重复调用外部服务。
        if state.route_optimization_status == "candidate_pending":
            baseline_plan = state.route_optimization_baseline_plan
            baseline_routes = state.route_optimization_baseline_routes
            baseline_quality = state.route_optimization_baseline_quality
            baseline_fingerprint = state.route_optimization_baseline_fingerprint
            candidate = state.route_optimization_candidate
            if not all((baseline_plan, baseline_routes, baseline_quality, baseline_fingerprint, candidate)):
                raise ValueError("Pending route optimization is missing baseline data")

            assert baseline_quality is not None
            assert candidate is not None
            actual_improvement = route_quality_improvement_percent(
                baseline_quality,
                report,
            )
            accepted = is_route_quality_improvement(
                baseline_quality,
                report,
                min_improvement_percent=self.route_optimization_min_improvement_percent,
            )
            candidate_fingerprint = current_fingerprint
            if accepted:
                status = "accepted"
                outcome_reason = (
                    "Candidate accepted after real route quality improved by "
                    f"{actual_improvement:.2f}%"
                )
            else:
                status = "reverted"
                outcome_reason = (
                    "Candidate reverted because real route improvement was below "
                    f"{self.route_optimization_min_improvement_percent:.2f}% or worsened hard failures"
                )
                state.trip_plan = baseline_plan.model_copy(deep=True)
                state.route_estimates = deepcopy(baseline_routes)
                state.route_plan_fingerprint = baseline_fingerprint
                state.route_quality_report = baseline_quality.model_copy(deep=True)
                state.route_quality_plan_fingerprint = baseline_fingerprint
                state.evaluation_input_fingerprints["route_quality"] = (
                    route_quality_input_fingerprint(
                        state.request, state.trip_plan, state.route_estimates
                    )
                )
                state.schedule_optimization_status = "not_started"
                self._refresh_schedule_quality(state)
                self._synchronize_evaluation_fingerprints(state)

            state.route_optimization_history.append(
                RouteOptimizationRecord(
                    attempt=state.route_optimization_count,
                    status=status,
                    reason=outcome_reason,
                    baseline_fingerprint=baseline_fingerprint,
                    candidate_fingerprint=candidate_fingerprint,
                    strategy=candidate.strategy,
                    changed_day_index=candidate.changed_day_index,
                    approximate_improvement_percent=(
                        candidate.approximate_improvement_percent
                    ),
                    actual_improvement_percent=actual_improvement,
                    baseline_cost=baseline_quality.optimization_cost,
                    candidate_cost=report.optimization_cost,
                )
            )
            state.route_optimization_status = "completed"
            self._clear_route_optimization_baseline(state)
            state.last_validation_result = None
            self._record_local_route_action(state, reason, lifetime_attempt)
            return

        max_attempts = state.execution_budget.max_route_optimization_attempts
        if state.route_optimization_count >= max_attempts:
            state.route_optimization_status = "skipped"
            state.route_optimization_history.append(
                RouteOptimizationRecord(
                    attempt=state.route_optimization_count,
                    status="skipped",
                    reason="Route optimization attempt budget is exhausted",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_route_action(state, reason, lifetime_attempt)
            return

        if not report.optimization_recommended:
            state.route_optimization_status = "skipped"
            state.route_optimization_history.append(
                RouteOptimizationRecord(
                    attempt=state.route_optimization_count,
                    status="skipped",
                    reason="Current route quality does not require reordering",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_route_action(state, reason, lifetime_attempt)
            return

        candidate = self.route_optimizer.optimize(state.trip_plan)
        if candidate is None:
            state.route_optimization_status = "skipped"
            state.route_optimization_history.append(
                RouteOptimizationRecord(
                    attempt=state.route_optimization_count,
                    status="skipped",
                    reason="No shorter deterministic geographic candidate was found",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_route_action(state, reason, lifetime_attempt)
            return

        state.route_optimization_baseline_plan = state.trip_plan.model_copy(deep=True)
        state.route_optimization_baseline_routes = deepcopy(state.route_estimates)
        state.route_optimization_baseline_quality = report.model_copy(deep=True)
        state.route_optimization_baseline_fingerprint = current_fingerprint
        state.route_optimization_candidate = candidate.model_copy(deep=True)
        state.trip_plan = candidate.plan.model_copy(deep=True)
        self._clear_commute_analysis(state, reset_optimization_count=False)
        state.plan_consistency_fingerprint = None
        state.route_optimization_count += 1
        state.route_optimization_status = "candidate_pending"
        state.route_estimates = None
        state.route_plan_fingerprint = None
        state.route_quality_report = None
        state.route_quality_plan_fingerprint = None
        state.schedule_quality_report = None
        state.schedule_quality_plan_fingerprint = None
        state.schedule_optimization_status = "not_started"
        state.schedule_optimization_candidate = None
        state.schedule_optimization_baseline_plan = None
        state.schedule_optimization_baseline_routes = None
        state.schedule_optimization_baseline_route_quality = None
        state.schedule_optimization_baseline_quality = None
        state.schedule_optimization_baseline_fingerprint = None
        self._clear_constraint_analysis(state, reset_optimization_count=False)
        state.last_validation_result = None
        self._record_local_route_action(state, reason, lifetime_attempt)

    def _refresh_commute_report(self, state: AgentState):
        """对当前真实路线执行分交通方式的单段通勤上限评估。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for commute evaluation")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        report = self.commute_evaluator.evaluate(
            state.request,
            state.trip_plan,
            state.route_estimates,
        )
        if report.plan_fingerprint != current_fingerprint:
            raise ValueError("Commute report does not match the current trip plan")
        previous_fingerprint = state.commute_plan_fingerprint
        state.commute_report = report
        state.commute_plan_fingerprint = current_fingerprint
        state.evaluation_input_fingerprints["commute"] = commute_input_fingerprint(
            state.request, state.trip_plan, state.route_estimates
        )
        if (
            report.optimization_recommended
            and previous_fingerprint != current_fingerprint
            and state.commute_optimization_status in {"completed", "skipped"}
        ):
            state.commute_optimization_status = "not_started"
        elif (
            state.commute_optimization_status == "not_started"
            and not report.optimization_recommended
        ):
            state.commute_optimization_status = "skipped"
        return report

    @staticmethod
    def _clear_commute_baseline(state: AgentState) -> None:
        state.commute_candidate = None
        state.commute_baseline_plan = None
        state.commute_baseline_routes = None
        state.commute_baseline_route_quality = None
        state.commute_baseline_report = None
        state.commute_baseline_schedule = None
        state.commute_baseline_constraint_report = None
        state.commute_baseline_route_fingerprint = None
        state.commute_baseline_constraint_fingerprint = None

    @staticmethod
    def _clear_commute_analysis(
        state: AgentState,
        *,
        reset_optimization_count: bool,
    ) -> None:
        TripOrchestrator._invalidate_evaluation_fingerprints(
            state, "commute", "validation"
        )
        state.commute_report = None
        state.commute_plan_fingerprint = None
        state.commute_optimization_status = "not_started"
        state.commute_supplement_query = None
        TripOrchestrator._clear_commute_baseline(state)
        if reset_optimization_count:
            state.commute_replacement_count = 0
            state.commute_supplement_search_count = 0
            state.commute_excluded_candidate_identities = []

    @staticmethod
    def _record_local_commute_action(
        state: AgentState,
        action: AgentAction,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=action,
                reason=reason,
                attempt=lifetime_attempt,
                success=True,
            )
        )

    def _handle_failed_attraction_supplement(
        self,
        state: AgentState,
        error: str,
    ) -> None:
        """高德周边补搜失败时按调用场景有界降级，保留当前有效行程。"""

        query = state.commute_supplement_query
        if state.content_refill_status == "supplement_needed":
            if query is not None:
                state.content_refill_supplement_search_count += 1
                state.content_refill_supplement_history.append(
                    CommuteSupplementRecord(
                        attempt=state.content_refill_supplement_search_count,
                        status="failed",
                        reason=(
                            "Nearby candidate search for minimum content failed; "
                            "kept current plan"
                        ),
                        target_attraction_name=query.target_attraction_name,
                        day_index=query.day_index,
                        attraction_index=query.attraction_index,
                        anchor_names=query.anchor_names,
                        center_longitude=query.center.longitude,
                        center_latitude=query.center.latitude,
                        radius_meters=query.radius_meters,
                        error=error,
                    )
                )
            state.commute_supplement_query = None
            state.content_refill_status = "not_started"
            return

        if query is not None:
            state.commute_supplement_search_count += 1
            state.commute_supplement_history.append(
                CommuteSupplementRecord(
                    attempt=state.commute_supplement_search_count,
                    status="failed",
                    reason="Optional nearby candidate search failed; kept current plan",
                    target_attraction_name=query.target_attraction_name,
                    day_index=query.day_index,
                    attraction_index=query.attraction_index,
                    anchor_names=query.anchor_names,
                    center_longitude=query.center.longitude,
                    center_latitude=query.center.latitude,
                    radius_meters=query.radius_meters,
                    error=error,
                )
            )
        state.commute_supplement_query = None
        state.commute_optimization_status = "skipped"

    def _evaluate_commute(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        self._refresh_commute_report(state)
        self._record_local_commute_action(
            state,
            AgentAction.EVALUATE_COMMUTE,
            reason,
            lifetime_attempt,
        )

    def _replace_remote_attraction(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """生成或使用真实路线复验一个有界近距离景点替换候选。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for commute replacement")
        if any(
            item is None
            for item in (
                state.route_quality_report,
                state.commute_report,
                state.schedule_quality_report,
                state.constraint_report,
            )
        ):
            raise ValueError("Route, commute, schedule and constraint reports are required")

        if state.commute_optimization_status == "candidate_pending":
            baseline_plan = state.commute_baseline_plan
            baseline_routes = state.commute_baseline_routes
            baseline_route_quality = state.commute_baseline_route_quality
            baseline_report = state.commute_baseline_report
            baseline_schedule = state.commute_baseline_schedule
            baseline_constraint = state.commute_baseline_constraint_report
            baseline_route_fingerprint = state.commute_baseline_route_fingerprint
            baseline_constraint_fingerprint = (
                state.commute_baseline_constraint_fingerprint
            )
            candidate = state.commute_candidate
            if any(
                item is None
                for item in (
                    baseline_plan,
                    baseline_routes,
                    baseline_route_quality,
                    baseline_report,
                    baseline_schedule,
                    baseline_constraint,
                    baseline_route_fingerprint,
                    baseline_constraint_fingerprint,
                    candidate,
                )
            ):
                raise ValueError("Pending commute replacement is missing verification data")

            current_report = state.commute_report
            current_route_quality = state.route_quality_report
            current_schedule = state.schedule_quality_report
            current_constraint = state.constraint_report
            assert baseline_plan is not None
            assert baseline_routes is not None
            assert baseline_route_quality is not None
            assert baseline_report is not None
            assert baseline_schedule is not None
            assert baseline_constraint is not None
            assert baseline_route_fingerprint is not None
            assert baseline_constraint_fingerprint is not None
            assert candidate is not None
            assert current_report is not None
            assert current_route_quality is not None
            assert current_schedule is not None
            assert current_constraint is not None

            route_safe = (
                current_route_quality.unavailable_legs
                <= baseline_route_quality.unavailable_legs
            )
            commute_improved = (
                current_report.excessive_segment_count
                < baseline_report.excessive_segment_count
                and current_report.max_duration_seconds
                < baseline_report.max_duration_seconds
            )
            normalized_replacement = "".join(
                candidate.replacement_attraction_name.lower().split()
            )
            replacement_still_excessive = any(
                normalized_replacement
                in {
                    "".join(issue.origin_name.lower().split()),
                    "".join(issue.destination_name.lower().split()),
                    "".join(issue.target_attraction_name.lower().split()),
                }
                for issue in current_report.issues
            )
            schedule_safe = (
                current_schedule.total_overtime_minutes == 0
                and current_schedule.infeasible_days == 0
            )
            constraints_safe = current_constraint.error_count == 0
            attraction_count_stable = (
                count_attractions(state.trip_plan) == count_attractions(baseline_plan)
            )
            accepted = (
                route_safe
                and commute_improved
                and not replacement_still_excessive
                and schedule_safe
                and constraints_safe
                and attraction_count_stable
            )
            candidate_fingerprint = plan_route_fingerprint(
                state.request,
                state.trip_plan,
            )

            if accepted:
                outcome_status = "accepted"
                outcome_reason = (
                    "Replacement accepted after real routes reduced excessive "
                    "single-leg commutes and schedule constraints remained feasible"
                )
                if current_report.optimization_recommended:
                    state.commute_optimization_status = (
                        "not_started"
                        if state.commute_replacement_count
                        < state.execution_budget.max_commute_replacement_attempts
                        else "skipped"
                    )
                else:
                    state.commute_optimization_status = "completed"
                state.plan_consistency_fingerprint = None
                state.last_validation_result = None
            else:
                outcome_status = "reverted"
                outcome_reason = (
                    "Replacement reverted because real routes did not materially "
                    "reduce the excessive commute or feasibility checks regressed"
                )
                identity = content_attraction_identity(
                    poi_id=candidate.replacement_attraction_id,
                    name=candidate.replacement_attraction_name,
                )
                if identity not in state.commute_excluded_candidate_identities:
                    state.commute_excluded_candidate_identities.append(identity)
                state.trip_plan = baseline_plan.model_copy(deep=True)
                state.route_estimates = deepcopy(baseline_routes)
                state.route_plan_fingerprint = baseline_route_fingerprint
                state.route_quality_report = baseline_route_quality.model_copy(deep=True)
                state.route_quality_plan_fingerprint = baseline_route_fingerprint
                state.commute_report = baseline_report.model_copy(deep=True)
                state.commute_plan_fingerprint = baseline_route_fingerprint
                state.schedule_quality_report = baseline_schedule.model_copy(deep=True)
                state.schedule_quality_plan_fingerprint = baseline_route_fingerprint
                state.constraint_report = baseline_constraint.model_copy(deep=True)
                state.constraint_plan_fingerprint = baseline_constraint_fingerprint
                self._synchronize_evaluation_fingerprints(state)
                state.commute_optimization_status = (
                    "not_started"
                    if state.commute_replacement_count
                    < state.execution_budget.max_commute_replacement_attempts
                    else "skipped"
                )
                state.plan_consistency_fingerprint = None
                state.last_validation_result = None

            state.commute_replacement_history.append(
                CommuteReplacementRecord(
                    attempt=state.commute_replacement_count,
                    status=outcome_status,
                    reason=outcome_reason,
                    day_index=candidate.day_index,
                    attraction_index=candidate.attraction_index,
                    replaced_attraction_name=candidate.replaced_attraction_name,
                    replacement_attraction_name=candidate.replacement_attraction_name,
                    replacement_attraction_id=candidate.replacement_attraction_id,
                    baseline_excessive_segments=(
                        baseline_report.excessive_segment_count
                    ),
                    candidate_excessive_segments=(
                        current_report.excessive_segment_count
                    ),
                    baseline_max_duration_seconds=(
                        baseline_report.max_duration_seconds
                    ),
                    candidate_max_duration_seconds=(
                        current_report.max_duration_seconds
                    ),
                    baseline_fingerprint=baseline_route_fingerprint,
                    candidate_fingerprint=candidate_fingerprint,
                )
            )
            self._clear_commute_baseline(state)
            self._record_local_commute_action(
                state,
                AgentAction.REPLACE_REMOTE_ATTRACTION,
                reason,
                lifetime_attempt,
            )
            return

        if (
            state.commute_replacement_count
            >= state.execution_budget.max_commute_replacement_attempts
        ):
            state.commute_optimization_status = "skipped"
            state.commute_replacement_history.append(
                CommuteReplacementRecord(
                    attempt=state.commute_replacement_count,
                    status="skipped",
                    reason="Commute replacement attempt budget is exhausted",
                    baseline_excessive_segments=(
                        state.commute_report.excessive_segment_count
                    ),
                    candidate_excessive_segments=(
                        state.commute_report.excessive_segment_count
                    ),
                    baseline_max_duration_seconds=(
                        state.commute_report.max_duration_seconds
                    ),
                    candidate_max_duration_seconds=(
                        state.commute_report.max_duration_seconds
                    ),
                    baseline_fingerprint=plan_route_fingerprint(
                        state.request,
                        state.trip_plan,
                    ),
                )
            )
            self._record_local_commute_action(
                state,
                AgentAction.REPLACE_REMOTE_ATTRACTION,
                reason,
                lifetime_attempt,
            )
            return

        candidate = self.commute_optimizer.optimize(
            state.request,
            state.trip_plan,
            state.commute_report,
            attractions=state.attractions,
            excluded_candidate_identities=set(
                state.commute_excluded_candidate_identities
            ),
        )
        if candidate is None:
            if (
                state.commute_supplement_search_count
                < state.execution_budget.max_commute_supplement_searches
            ):
                query = self.commute_supplementer.build_query(
                    state.request,
                    state.trip_plan,
                    state.commute_report,
                    search_index=state.commute_supplement_search_count,
                )
                if query is not None:
                    state.commute_supplement_query = query
                    state.commute_optimization_status = "supplement_needed"
                    self._record_local_commute_action(
                        state,
                        AgentAction.REPLACE_REMOTE_ATTRACTION,
                        reason,
                        lifetime_attempt,
                    )
                    return

            state.commute_optimization_status = "skipped"
            state.commute_replacement_history.append(
                CommuteReplacementRecord(
                    attempt=state.commute_replacement_count,
                    status="skipped",
                    reason="No nearer unused Amap candidate was found",
                    baseline_excessive_segments=(
                        state.commute_report.excessive_segment_count
                    ),
                    candidate_excessive_segments=(
                        state.commute_report.excessive_segment_count
                    ),
                    baseline_max_duration_seconds=(
                        state.commute_report.max_duration_seconds
                    ),
                    candidate_max_duration_seconds=(
                        state.commute_report.max_duration_seconds
                    ),
                    baseline_fingerprint=plan_route_fingerprint(
                        state.request,
                        state.trip_plan,
                    ),
                )
            )
            self._record_local_commute_action(
                state,
                AgentAction.REPLACE_REMOTE_ATTRACTION,
                reason,
                lifetime_attempt,
            )
            return

        assert state.route_quality_report is not None
        assert state.schedule_quality_report is not None
        assert state.constraint_report is not None
        baseline_route_fingerprint = plan_route_fingerprint(
            state.request,
            state.trip_plan,
        )
        baseline_constraint_fingerprint = constraint_plan_fingerprint(
            state.request,
            state.trip_plan,
        )
        state.commute_baseline_plan = state.trip_plan.model_copy(deep=True)
        state.commute_baseline_routes = deepcopy(state.route_estimates)
        state.commute_baseline_route_quality = (
            state.route_quality_report.model_copy(deep=True)
        )
        state.commute_baseline_report = state.commute_report.model_copy(deep=True)
        state.commute_baseline_schedule = (
            state.schedule_quality_report.model_copy(deep=True)
        )
        state.commute_baseline_constraint_report = (
            state.constraint_report.model_copy(deep=True)
        )
        state.commute_baseline_route_fingerprint = baseline_route_fingerprint
        state.commute_baseline_constraint_fingerprint = (
            baseline_constraint_fingerprint
        )
        state.commute_candidate = candidate.model_copy(deep=True)
        state.trip_plan = candidate.plan.model_copy(deep=True)
        state.commute_replacement_count += 1
        state.commute_optimization_status = "candidate_pending"
        state.route_estimates = None
        state.route_plan_fingerprint = None
        state.route_quality_report = None
        state.route_quality_plan_fingerprint = None
        state.commute_report = None
        state.commute_plan_fingerprint = None
        state.schedule_quality_report = None
        state.schedule_quality_plan_fingerprint = None
        state.constraint_report = None
        state.constraint_plan_fingerprint = None
        state.plan_consistency_fingerprint = None
        state.last_validation_result = None
        self._record_local_commute_action(
            state,
            AgentAction.REPLACE_REMOTE_ATTRACTION,
            reason,
            lifetime_attempt,
        )

    def _refresh_schedule_quality(self, state: AgentState):
        """使用已持久化的路线快照构建当前完整时间轴报告。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for schedule scoring")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        route_result = RouteEstimateResult.model_validate(state.route_estimates)
        if route_result.plan_fingerprint != current_fingerprint:
            raise ValueError("Route estimates do not match the current trip plan")
        report = self.schedule_evaluator.evaluate(
            state.request,
            state.trip_plan,
            route_result,
        )
        state.schedule_quality_report = report
        state.schedule_quality_plan_fingerprint = current_fingerprint
        state.evaluation_input_fingerprints["schedule"] = schedule_input_fingerprint(
            state.request, state.trip_plan, state.route_estimates
        )
        if (
            state.schedule_optimization_status == "not_started"
            and not report.optimization_recommended
        ):
            state.schedule_optimization_status = "skipped"
        return report

    @staticmethod
    def _schedule_improvement_percent(before, after) -> float:
        if before.optimization_cost <= 0:
            return 0.0
        return round(
            (before.optimization_cost - after.optimization_cost)
            / before.optimization_cost
            * 100.0,
            2,
        )

    @staticmethod
    def _clear_schedule_optimization_baseline(state: AgentState) -> None:
        state.schedule_optimization_candidate = None
        state.schedule_optimization_baseline_plan = None
        state.schedule_optimization_baseline_routes = None
        state.schedule_optimization_baseline_route_quality = None
        state.schedule_optimization_baseline_quality = None
        state.schedule_optimization_baseline_fingerprint = None

    @staticmethod
    def _record_local_schedule_action(
        state: AgentState,
        action: AgentAction,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=action,
                reason=reason,
                attempt=lifetime_attempt,
                success=True,
            )
        )

    def _evaluate_schedule(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """不调用外部工具，对过期或旧版本检查点进行本地时间轴评估。"""

        self._refresh_schedule_quality(state)
        self._record_local_schedule_action(
            state,
            AgentAction.EVALUATE_SCHEDULE,
            reason,
            lifetime_attempt,
        )

    def _optimize_schedule(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """生成或复验一个有界的跨日时间轴优化候选。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for schedule optimization")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        report = state.schedule_quality_report
        if (
            report is None
            or state.schedule_quality_plan_fingerprint != current_fingerprint
        ):
            report = self._refresh_schedule_quality(state)

        # 候选已经取得新的高德路线后，进入第二轮真实结果复验。
        if state.schedule_optimization_status == "candidate_pending":
            baseline_plan = state.schedule_optimization_baseline_plan
            baseline_routes = state.schedule_optimization_baseline_routes
            baseline_route_quality = state.schedule_optimization_baseline_route_quality
            baseline_quality = state.schedule_optimization_baseline_quality
            baseline_fingerprint = state.schedule_optimization_baseline_fingerprint
            candidate = state.schedule_optimization_candidate
            if any(
                item is None
                for item in (
                    baseline_plan,
                    baseline_routes,
                    baseline_route_quality,
                    baseline_quality,
                    baseline_fingerprint,
                    candidate,
                )
            ):
                raise ValueError("Pending schedule optimization is missing baseline data")
            if state.route_quality_report is None:
                raise ValueError("Candidate route quality is missing")

            assert baseline_quality is not None
            assert baseline_route_quality is not None
            assert candidate is not None
            candidate_cost = report.optimization_cost
            actual_improvement = self._schedule_improvement_percent(
                baseline_quality,
                report,
            )
            route_quality_safe = (
                state.route_quality_report.unavailable_legs
                <= baseline_route_quality.unavailable_legs
                and state.route_quality_report.excessive_duration_legs
                <= baseline_route_quality.excessive_duration_legs
            )
            accepted = (
                report.optimization_cost < baseline_quality.optimization_cost
                and actual_improvement
                >= self.schedule_optimization_min_improvement_percent
                and report.total_overtime_minutes
                <= baseline_quality.total_overtime_minutes
                and report.infeasible_days <= baseline_quality.infeasible_days
                and report.fallback_route_legs <= baseline_quality.fallback_route_legs
                and route_quality_safe
            )
            candidate_fingerprint = current_fingerprint
            if accepted:
                status = "accepted"
                outcome_reason = (
                    "Candidate accepted after real schedule cost improved by "
                    f"{actual_improvement:.2f}%"
                )
            else:
                status = "reverted"
                outcome_reason = (
                    "Candidate reverted because real schedule improvement was below "
                    f"{self.schedule_optimization_min_improvement_percent:.2f}% "
                    "or route/schedule hard metrics worsened"
                )
                state.trip_plan = baseline_plan.model_copy(deep=True)
                state.route_estimates = deepcopy(baseline_routes)
                state.route_plan_fingerprint = baseline_fingerprint
                state.route_quality_report = baseline_route_quality.model_copy(deep=True)
                state.route_quality_plan_fingerprint = baseline_fingerprint
                state.schedule_quality_report = baseline_quality.model_copy(deep=True)
                state.schedule_quality_plan_fingerprint = baseline_fingerprint
                self._synchronize_evaluation_fingerprints(state)

            state.schedule_optimization_history.append(
                ScheduleOptimizationRecord(
                    attempt=state.schedule_optimization_count,
                    status=status,
                    reason=outcome_reason,
                    baseline_fingerprint=baseline_fingerprint,
                    candidate_fingerprint=candidate_fingerprint,
                    strategy=candidate.strategy,
                    source_day_index=candidate.source_day_index,
                    target_day_index=candidate.target_day_index,
                    moved_attraction_name=candidate.moved_attraction_name,
                    removed_attraction_names=candidate.removed_attraction_names,
                    approximate_improvement_percent=(
                        candidate.approximate_improvement_percent
                    ),
                    actual_improvement_percent=actual_improvement,
                    baseline_cost=baseline_quality.optimization_cost,
                    candidate_cost=candidate_cost,
                )
            )
            state.schedule_optimization_status = "completed"
            self._clear_schedule_optimization_baseline(state)
            state.last_validation_result = None
            self._record_local_schedule_action(
                state,
                AgentAction.OPTIMIZE_SCHEDULE,
                reason,
                lifetime_attempt,
            )
            return

        max_attempts = state.execution_budget.max_schedule_optimization_attempts
        if state.schedule_optimization_count >= max_attempts:
            state.schedule_optimization_status = "skipped"
            state.schedule_optimization_history.append(
                ScheduleOptimizationRecord(
                    attempt=state.schedule_optimization_count,
                    status="skipped",
                    reason="Schedule optimization attempt budget is exhausted",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_schedule_action(
                state,
                AgentAction.OPTIMIZE_SCHEDULE,
                reason,
                lifetime_attempt,
            )
            return

        if not report.optimization_recommended:
            state.schedule_optimization_status = "skipped"
            state.schedule_optimization_history.append(
                ScheduleOptimizationRecord(
                    attempt=state.schedule_optimization_count,
                    status="skipped",
                    reason="Current schedule fits within the daily time window",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_schedule_action(
                state,
                AgentAction.OPTIMIZE_SCHEDULE,
                reason,
                lifetime_attempt,
            )
            return

        candidate = self.schedule_optimizer.optimize(
            state.request,
            state.trip_plan,
            report,
        )
        if candidate is None:
            state.schedule_optimization_status = "skipped"
            state.schedule_optimization_history.append(
                ScheduleOptimizationRecord(
                    attempt=state.schedule_optimization_count,
                    status="skipped",
                    reason="No lower-cost bounded cross-day candidate was found",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_schedule_action(
                state,
                AgentAction.OPTIMIZE_SCHEDULE,
                reason,
                lifetime_attempt,
            )
            return

        if state.route_quality_report is None:
            raise ValueError("Baseline route quality is required for schedule optimization")
        state.schedule_optimization_baseline_plan = state.trip_plan.model_copy(deep=True)
        state.schedule_optimization_baseline_routes = deepcopy(state.route_estimates)
        state.schedule_optimization_baseline_route_quality = (
            state.route_quality_report.model_copy(deep=True)
        )
        state.schedule_optimization_baseline_quality = report.model_copy(deep=True)
        state.schedule_optimization_baseline_fingerprint = current_fingerprint
        state.schedule_optimization_candidate = candidate.model_copy(deep=True)
        state.trip_plan = candidate.plan.model_copy(deep=True)
        self._clear_commute_analysis(state, reset_optimization_count=False)
        state.plan_consistency_fingerprint = None
        state.schedule_optimization_count += 1
        state.schedule_optimization_status = "candidate_pending"
        # 路线排序优化已经结束，因此这里只清理路线快照，
        # 防止跨日移动后的行程重新进入更早的路线排序阶段。
        state.route_estimates = None
        state.route_plan_fingerprint = None
        state.route_quality_report = None
        state.route_quality_plan_fingerprint = None
        state.schedule_quality_report = None
        state.schedule_quality_plan_fingerprint = None
        self._clear_constraint_analysis(state, reset_optimization_count=False)
        state.last_validation_result = None
        self._record_local_schedule_action(
            state,
            AgentAction.OPTIMIZE_SCHEDULE,
            reason,
            lifetime_attempt,
        )

    def _refresh_constraint_report(self, state: AgentState):
        """使用已持久化事实和时间轴评估当前行程可执行性。"""

        if state.trip_plan is None or state.schedule_quality_report is None:
            raise ValueError("Trip plan and schedule report are required for constraint evaluation")
        current_fingerprint = constraint_plan_fingerprint(state.request, state.trip_plan)
        report = self.constraint_evaluator.evaluate(
            state.request,
            state.trip_plan,
            state.schedule_quality_report,
            attractions=state.attractions,
            weather=state.weather,
        )
        if report.plan_fingerprint != current_fingerprint:
            raise ValueError("Constraint report does not match the current trip plan")
        state.constraint_report = report
        state.constraint_plan_fingerprint = current_fingerprint
        state.evaluation_input_fingerprints["constraints"] = (
            constraint_input_fingerprint(
                state.request,
                state.trip_plan,
                state.schedule_quality_report,
                state.attractions,
                state.weather,
            )
        )
        if (
            state.constraint_optimization_status == "not_started"
            and not report.optimization_recommended
        ):
            state.constraint_optimization_status = "skipped"
        return report

    @staticmethod
    def _constraint_improvement_percent(before, after) -> float:
        if before.optimization_cost <= 0:
            return 0.0
        return round(
            (before.optimization_cost - after.optimization_cost)
            / before.optimization_cost
            * 100.0,
            2,
        )

    @staticmethod
    def _clear_constraint_optimization_baseline(state: AgentState) -> None:
        state.constraint_optimization_candidate = None
        state.constraint_optimization_baseline_plan = None
        state.constraint_optimization_baseline_routes = None
        state.constraint_optimization_baseline_route_quality = None
        state.constraint_optimization_baseline_schedule = None
        state.constraint_optimization_baseline_report = None
        state.constraint_optimization_baseline_fingerprint = None

    @staticmethod
    def _record_local_constraint_action(
        state: AgentState,
        action: AgentAction,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=action,
                reason=reason,
                attempt=lifetime_attempt,
                success=True,
            )
        )

    def _evaluate_constraints(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """不调用外部服务，评估真实场景下的行程执行约束。"""

        self._refresh_constraint_report(state)
        self._record_local_constraint_action(
            state,
            AgentAction.EVALUATE_CONSTRAINTS,
            reason,
            lifetime_attempt,
        )

    def _optimize_constraints(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """生成或复验一个有界、确定性的约束冲突修复候选。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for constraint optimization")
        if state.route_quality_report is None or state.schedule_quality_report is None:
            raise ValueError("Route and schedule quality are required for constraint optimization")

        current_fingerprint = constraint_plan_fingerprint(state.request, state.trip_plan)
        report = state.constraint_report
        if report is None or state.constraint_plan_fingerprint != current_fingerprint:
            report = self._refresh_constraint_report(state)

        if state.constraint_optimization_status == "candidate_pending":
            baseline_plan = state.constraint_optimization_baseline_plan
            baseline_routes = state.constraint_optimization_baseline_routes
            baseline_route_quality = state.constraint_optimization_baseline_route_quality
            baseline_schedule = state.constraint_optimization_baseline_schedule
            baseline_report = state.constraint_optimization_baseline_report
            baseline_fingerprint = state.constraint_optimization_baseline_fingerprint
            candidate = state.constraint_optimization_candidate
            if any(
                item is None
                for item in (
                    baseline_plan,
                    baseline_routes,
                    baseline_route_quality,
                    baseline_schedule,
                    baseline_report,
                    baseline_fingerprint,
                    candidate,
                )
            ):
                raise ValueError("Pending constraint optimization is missing baseline data")

            assert baseline_plan is not None
            assert baseline_route_quality is not None
            assert baseline_schedule is not None
            assert baseline_report is not None
            assert baseline_fingerprint is not None
            assert candidate is not None
            actual_improvement = self._constraint_improvement_percent(
                baseline_report,
                report,
            )
            route_quality_safe = (
                state.route_quality_report.unavailable_legs
                <= baseline_route_quality.unavailable_legs
                and state.route_quality_report.excessive_duration_legs
                <= baseline_route_quality.excessive_duration_legs
            )
            schedule_safe = (
                state.schedule_quality_report.total_overtime_minutes
                <= baseline_schedule.total_overtime_minutes
                and state.schedule_quality_report.infeasible_days
                <= baseline_schedule.infeasible_days
                and state.schedule_quality_report.fallback_route_legs
                <= baseline_schedule.fallback_route_legs
            )
            accepted = (
                report.optimization_cost < baseline_report.optimization_cost
                and actual_improvement
                >= self.constraint_optimization_min_improvement_percent
                and report.error_count <= baseline_report.error_count
                and route_quality_safe
                and schedule_safe
            )
            candidate_fingerprint = current_fingerprint
            if accepted:
                status = "accepted"
                outcome_reason = (
                    "Candidate accepted after verified constraint cost improved by "
                    f"{actual_improvement:.2f}%"
                )
            else:
                status = "reverted"
                outcome_reason = (
                    "Candidate reverted because verified constraint improvement was below "
                    f"{self.constraint_optimization_min_improvement_percent:.2f}% "
                    "or route/schedule hard metrics worsened"
                )
                route_fingerprint = plan_route_fingerprint(state.request, baseline_plan)
                state.trip_plan = baseline_plan.model_copy(deep=True)
                state.route_estimates = deepcopy(baseline_routes)
                state.route_plan_fingerprint = route_fingerprint
                state.route_quality_report = baseline_route_quality.model_copy(deep=True)
                state.route_quality_plan_fingerprint = route_fingerprint
                state.schedule_quality_report = baseline_schedule.model_copy(deep=True)
                state.schedule_quality_plan_fingerprint = route_fingerprint
                state.constraint_report = baseline_report.model_copy(deep=True)
                state.constraint_plan_fingerprint = baseline_fingerprint
                self._synchronize_evaluation_fingerprints(state)

            state.constraint_optimization_history.append(
                ConstraintOptimizationRecord(
                    attempt=state.constraint_optimization_count,
                    status=status,
                    reason=outcome_reason,
                    baseline_fingerprint=baseline_fingerprint,
                    candidate_fingerprint=candidate_fingerprint,
                    strategy=candidate.strategy,
                    source_day_index=candidate.source_day_index,
                    target_day_index=candidate.target_day_index,
                    moved_attraction_name=candidate.moved_attraction_name,
                    removed_attraction_names=candidate.removed_attraction_names,
                    approximate_improvement_percent=(
                        candidate.approximate_improvement_percent
                    ),
                    actual_improvement_percent=actual_improvement,
                    baseline_cost=baseline_report.optimization_cost,
                    candidate_cost=report.optimization_cost,
                )
            )
            state.constraint_optimization_status = "completed"
            self._clear_constraint_optimization_baseline(state)
            state.last_validation_result = None
            self._record_local_constraint_action(
                state,
                AgentAction.OPTIMIZE_CONSTRAINTS,
                reason,
                lifetime_attempt,
            )
            return

        max_attempts = state.execution_budget.max_constraint_optimization_attempts
        if state.constraint_optimization_count >= max_attempts:
            state.constraint_optimization_status = "skipped"
            state.constraint_optimization_history.append(
                ConstraintOptimizationRecord(
                    attempt=state.constraint_optimization_count,
                    status="skipped",
                    reason="Constraint optimization attempt budget is exhausted",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_constraint_action(
                state,
                AgentAction.OPTIMIZE_CONSTRAINTS,
                reason,
                lifetime_attempt,
            )
            return

        if not report.optimization_recommended:
            state.constraint_optimization_status = "skipped"
            state.constraint_optimization_history.append(
                ConstraintOptimizationRecord(
                    attempt=state.constraint_optimization_count,
                    status="skipped",
                    reason="Current plan has no repairable execution constraint",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_constraint_action(
                state,
                AgentAction.OPTIMIZE_CONSTRAINTS,
                reason,
                lifetime_attempt,
            )
            return

        candidate = self.constraint_optimizer.optimize(
            state.request,
            state.trip_plan,
            report,
            attractions=state.attractions,
            weather=state.weather,
        )
        if candidate is None:
            state.constraint_optimization_status = "skipped"
            state.constraint_optimization_history.append(
                ConstraintOptimizationRecord(
                    attempt=state.constraint_optimization_count,
                    status="skipped",
                    reason="No lower-cost bounded constraint candidate was found",
                    baseline_fingerprint=current_fingerprint,
                    baseline_cost=report.optimization_cost,
                )
            )
            self._record_local_constraint_action(
                state,
                AgentAction.OPTIMIZE_CONSTRAINTS,
                reason,
                lifetime_attempt,
            )
            return

        state.constraint_optimization_baseline_plan = state.trip_plan.model_copy(deep=True)
        state.constraint_optimization_baseline_routes = deepcopy(state.route_estimates)
        state.constraint_optimization_baseline_route_quality = (
            state.route_quality_report.model_copy(deep=True)
        )
        state.constraint_optimization_baseline_schedule = (
            state.schedule_quality_report.model_copy(deep=True)
        )
        state.constraint_optimization_baseline_report = report.model_copy(deep=True)
        state.constraint_optimization_baseline_fingerprint = current_fingerprint
        state.constraint_optimization_candidate = candidate.model_copy(deep=True)
        state.trip_plan = candidate.plan.model_copy(deep=True)
        self._clear_commute_analysis(state, reset_optimization_count=False)
        state.plan_consistency_fingerprint = None
        state.constraint_optimization_count += 1
        state.constraint_optimization_status = "candidate_pending"
        # 候选被接受前必须重新构建高德真实路线和完整时间轴。
        state.route_estimates = None
        state.route_plan_fingerprint = None
        state.route_quality_report = None
        state.route_quality_plan_fingerprint = None
        state.schedule_quality_report = None
        state.schedule_quality_plan_fingerprint = None
        state.constraint_report = None
        state.constraint_plan_fingerprint = None
        state.last_validation_result = None
        self._record_local_constraint_action(
            state,
            AgentAction.OPTIMIZE_CONSTRAINTS,
            reason,
            lifetime_attempt,
        )

    @staticmethod
    def _clear_content_refill_baseline(state: AgentState) -> None:
        state.content_refill_candidate = None
        state.content_refill_baseline_plan = None
        state.content_refill_baseline_routes = None
        state.content_refill_baseline_route_quality = None
        state.content_refill_baseline_schedule = None
        state.content_refill_baseline_constraint_report = None
        state.content_refill_baseline_route_fingerprint = None
        state.content_refill_baseline_constraint_fingerprint = None

    @staticmethod
    def _record_local_content_action(
        state: AgentState,
        action: AgentAction,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=action,
                reason=reason,
                attempt=lifetime_attempt,
                success=True,
            )
        )

    def _refill_attractions(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """生成或复验一个使用附近未使用 POI 的最低景点回填候选。"""

        if state.trip_plan is None:
            raise ValueError("Trip plan is required for attraction refill")
        required = min(
            state.execution_budget.minimum_total_attractions,
            max(1, state.request.travel_days),
        )

        if state.content_refill_status == "candidate_pending":
            baseline_plan = state.content_refill_baseline_plan
            baseline_routes = state.content_refill_baseline_routes
            baseline_route_quality = state.content_refill_baseline_route_quality
            baseline_schedule = state.content_refill_baseline_schedule
            baseline_constraint = state.content_refill_baseline_constraint_report
            baseline_route_fingerprint = state.content_refill_baseline_route_fingerprint
            baseline_constraint_fingerprint = (
                state.content_refill_baseline_constraint_fingerprint
            )
            candidate = state.content_refill_candidate
            if any(
                item is None
                for item in (
                    baseline_plan,
                    baseline_routes,
                    baseline_route_quality,
                    baseline_schedule,
                    baseline_constraint,
                    baseline_route_fingerprint,
                    baseline_constraint_fingerprint,
                    candidate,
                    state.route_quality_report,
                    state.schedule_quality_report,
                    state.constraint_report,
                )
            ):
                raise ValueError("Pending content refill is missing verification data")

            assert baseline_plan is not None
            assert baseline_routes is not None
            assert baseline_route_quality is not None
            assert baseline_schedule is not None
            assert baseline_constraint is not None
            assert baseline_route_fingerprint is not None
            assert baseline_constraint_fingerprint is not None
            assert candidate is not None
            assert state.route_quality_report is not None
            assert state.schedule_quality_report is not None
            assert state.constraint_report is not None

            candidate_route_fingerprint = plan_route_fingerprint(
                state.request,
                state.trip_plan,
            )
            route_safe = (
                state.route_quality_report.unavailable_legs
                <= baseline_route_quality.unavailable_legs
                and state.route_quality_report.excessive_duration_legs
                <= baseline_route_quality.excessive_duration_legs
            )
            schedule_safe = (
                state.schedule_quality_report.total_overtime_minutes == 0
                and state.schedule_quality_report.infeasible_days == 0
            )
            constraints_safe = state.constraint_report.error_count == 0
            minimum_met = count_attractions(state.trip_plan) >= required
            accepted = route_safe and schedule_safe and constraints_safe and minimum_met

            if accepted:
                status = "accepted"
                outcome_reason = (
                    "Refill accepted after real routes, schedule and constraints "
                    "confirmed the minimum attraction count"
                )
                state.content_refill_status = "completed"
            else:
                status = "reverted"
                outcome_reason = (
                    "Refill reverted because minimum content or verified route, "
                    "schedule and constraint safety requirements were not met"
                )
                for name, poi_id in zip(
                    candidate.added_attraction_names,
                    candidate.added_attraction_ids,
                ):
                    identity = content_attraction_identity(poi_id=poi_id, name=name)
                    if identity not in state.content_refill_excluded_identities:
                        state.content_refill_excluded_identities.append(identity)
                state.trip_plan = baseline_plan.model_copy(deep=True)
                state.route_estimates = deepcopy(baseline_routes)
                state.route_plan_fingerprint = baseline_route_fingerprint
                state.route_quality_report = baseline_route_quality.model_copy(deep=True)
                state.route_quality_plan_fingerprint = baseline_route_fingerprint
                state.schedule_quality_report = baseline_schedule.model_copy(deep=True)
                state.schedule_quality_plan_fingerprint = baseline_route_fingerprint
                state.constraint_report = baseline_constraint.model_copy(deep=True)
                state.constraint_plan_fingerprint = baseline_constraint_fingerprint
                self._synchronize_evaluation_fingerprints(state)
                state.content_refill_status = (
                    "not_started"
                    if state.content_refill_count
                    < state.execution_budget.max_content_refill_attempts
                    else "skipped"
                )

            state.content_refill_history.append(
                ContentRefillRecord(
                    attempt=state.content_refill_count,
                    status=status,
                    reason=outcome_reason,
                    added_attraction_names=candidate.added_attraction_names,
                    added_attraction_ids=candidate.added_attraction_ids,
                    target_day_indices=candidate.target_day_indices,
                    baseline_attraction_count=candidate.baseline_attraction_count,
                    candidate_attraction_count=candidate.candidate_attraction_count,
                    baseline_fingerprint=baseline_route_fingerprint,
                    candidate_fingerprint=candidate_route_fingerprint,
                )
            )
            self._clear_content_refill_baseline(state)
            state.plan_consistency_fingerprint = None
            state.last_validation_result = None
            self._record_local_content_action(
                state,
                AgentAction.REFILL_ATTRACTIONS,
                reason,
                lifetime_attempt,
            )
            return

        if count_attractions(state.trip_plan) >= required:
            state.content_refill_status = "completed"
            self._record_local_content_action(
                state,
                AgentAction.REFILL_ATTRACTIONS,
                reason,
                lifetime_attempt,
            )
            return

        max_attempts = state.execution_budget.max_content_refill_attempts
        if state.content_refill_count >= max_attempts:
            state.content_refill_status = "skipped"
            state.content_refill_history.append(
                ContentRefillRecord(
                    attempt=state.content_refill_count,
                    status="skipped",
                    reason="Content refill attempt budget is exhausted",
                    baseline_attraction_count=count_attractions(state.trip_plan),
                    candidate_attraction_count=count_attractions(state.trip_plan),
                    baseline_fingerprint=plan_route_fingerprint(
                        state.request,
                        state.trip_plan,
                    ),
                )
            )
            self._record_local_content_action(
                state,
                AgentAction.REFILL_ATTRACTIONS,
                reason,
                lifetime_attempt,
            )
            return

        if any(
            item is None
            for item in (
                state.route_estimates,
                state.route_quality_report,
                state.schedule_quality_report,
                state.constraint_report,
            )
        ):
            raise ValueError("Current route, schedule and constraint reports are required")

        candidate = self.content_refill_optimizer.optimize(
            state.request,
            state.trip_plan,
            attractions=state.attractions,
            excluded_candidate_identities=set(state.content_refill_excluded_identities),
        )
        if candidate is None:
            if (
                state.content_refill_supplement_search_count
                < state.execution_budget.max_commute_supplement_searches
            ):
                query = self.commute_supplementer.build_content_refill_query(
                    state.request,
                    state.trip_plan,
                    search_index=state.content_refill_supplement_search_count,
                )
                if query is not None:
                    state.commute_supplement_query = query
                    state.content_refill_status = "supplement_needed"
                    self._record_local_content_action(
                        state,
                        AgentAction.REFILL_ATTRACTIONS,
                        reason,
                        lifetime_attempt,
                    )
                    return

            state.content_refill_status = "skipped"
            state.content_refill_history.append(
                ContentRefillRecord(
                    attempt=state.content_refill_count,
                    status="skipped",
                    reason=(
                        "No schedule-feasible unused Amap candidate was found "
                        "after bounded nearby searches"
                    ),
                    baseline_attraction_count=count_attractions(state.trip_plan),
                    candidate_attraction_count=count_attractions(state.trip_plan),
                    baseline_fingerprint=plan_route_fingerprint(
                        state.request,
                        state.trip_plan,
                    ),
                )
            )
            self._record_local_content_action(
                state,
                AgentAction.REFILL_ATTRACTIONS,
                reason,
                lifetime_attempt,
            )
            return

        assert state.route_estimates is not None
        assert state.route_quality_report is not None
        assert state.schedule_quality_report is not None
        assert state.constraint_report is not None
        baseline_route_fingerprint = plan_route_fingerprint(
            state.request,
            state.trip_plan,
        )
        baseline_constraint_fingerprint = constraint_plan_fingerprint(
            state.request,
            state.trip_plan,
        )
        state.content_refill_baseline_plan = state.trip_plan.model_copy(deep=True)
        state.content_refill_baseline_routes = deepcopy(state.route_estimates)
        state.content_refill_baseline_route_quality = (
            state.route_quality_report.model_copy(deep=True)
        )
        state.content_refill_baseline_schedule = (
            state.schedule_quality_report.model_copy(deep=True)
        )
        state.content_refill_baseline_constraint_report = (
            state.constraint_report.model_copy(deep=True)
        )
        state.content_refill_baseline_route_fingerprint = baseline_route_fingerprint
        state.content_refill_baseline_constraint_fingerprint = (
            baseline_constraint_fingerprint
        )
        state.content_refill_candidate = candidate.model_copy(deep=True)
        state.trip_plan = candidate.plan.model_copy(deep=True)
        self._clear_commute_analysis(state, reset_optimization_count=False)
        state.content_refill_count += 1
        state.content_refill_status = "candidate_pending"
        state.route_estimates = None
        state.route_plan_fingerprint = None
        state.route_quality_report = None
        state.route_quality_plan_fingerprint = None
        state.schedule_quality_report = None
        state.schedule_quality_plan_fingerprint = None
        state.constraint_report = None
        state.constraint_plan_fingerprint = None
        state.plan_consistency_fingerprint = None
        state.last_validation_result = None
        self._record_local_content_action(
            state,
            AgentAction.REFILL_ATTRACTIONS,
            reason,
            lifetime_attempt,
        )

    def _rebuild_plan_content(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        """根据已经稳定的最终行程重建全部描述和费用字段。"""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for rebuilding")
        before_route_fingerprint = plan_route_fingerprint(
            state.request,
            state.trip_plan,
        )
        rebuilt = self.plan_consistency_rebuilder.rebuild(
            state.request,
            state.trip_plan,
            route_estimates=state.route_estimates,
            schedule_quality_report=state.schedule_quality_report,
            restaurants=state.restaurants,
        )
        after_route_fingerprint = plan_route_fingerprint(state.request, rebuilt)
        if after_route_fingerprint != before_route_fingerprint:
            raise ValueError("Consistency rebuild unexpectedly changed route inputs")
        state.trip_plan = rebuilt
        state.plan_consistency_fingerprint = plan_content_source_fingerprint(
            state.request,
            rebuilt,
            state.route_estimates,
            state.schedule_quality_report,
            state.restaurants,
        )
        state.plan_consistency_rebuild_count += 1
        state.last_validation_result = None
        self._record_local_content_action(
            state,
            AgentAction.REBUILD_PLAN_CONTENT,
            reason,
            lifetime_attempt,
        )

    def _validate_plan(
        self,
        state: AgentState,
        reason: str,
        lifetime_attempt: int,
    ) -> None:
        if state.trip_plan is None:
            raise ValueError("没有可校验的旅行计划")

        # 步骤 1：使用确定性规则检查日期、天数、来源、预算和路线合理性。
        result = self.validator.validate(
            state.request,
            state.trip_plan,
            attractions=state.attractions,
            weather=state.weather,
            hotels=state.hotels,
            route_estimates=state.route_estimates,
            schedule_quality_report=state.schedule_quality_report,
            constraint_report=state.constraint_report,
        )
        # 步骤 2：保存最新结果，同时追加历史记录，便于比较每次修复效果。
        state.last_validation_result = result
        state.evaluation_input_fingerprints["validation"] = (
            validation_input_fingerprint(
                state.request,
                state.trip_plan,
                state.attractions,
                state.weather,
                state.hotels,
                state.route_estimates,
                state.schedule_quality_report,
                state.constraint_report,
            )
        )
        state.validation_history.append(result)

        # 步骤 3：在每次确定性校验后执行交付分级。
        # 即使完整校验未通过，只要核心结构、路线、时间轴和约束均满足阈值，
        # 也可以保留非关键警告完成，避免继续进入无收益的 LLM 修复循环。
        state.acceptance_report = self.partial_acceptance_policy.evaluate(
            state,
            result,
        )

        # 步骤 4：完整校验通过或部分策略接受后，下一轮状态机会选择 FINISH。
        if result.valid or state.acceptance_report.accepted:
            state.action_history.append(
                ActionRecord(
                    step=state.current_step,
                    action=AgentAction.VALIDATE_PLAN,
                    reason=reason,
                    attempt=lifetime_attempt,
                    success=True,
                    validation_error_count=result.error_count,
                    validation_warning_count=result.warning_count,
                )
            )
            return

        # 步骤 5：未达到交付标准时，再判断是否适合交给 LLM 修复。
        can_repair = result.repairable and state.repair_count < state.max_repair_attempts
        error = result.error_summary()
        state.errors.append(f"validate_plan: {error}")
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=AgentAction.VALIDATE_PLAN,
                reason=reason,
                attempt=lifetime_attempt,
                success=False,
                error=error,
                error_type=ToolErrorType.INVALID_OUTPUT,
                retryable=can_repair,
                validation_error_count=result.error_count,
                validation_warning_count=result.warning_count,
            )
        )

        if not can_repair:
            state.status = "failed"
            if result.repairable and state.repair_count >= state.max_repair_attempts:
                error = f"自动修复次数已达到上限 {state.max_repair_attempts}: {error}"
            raise AgentActionError(
                AgentAction.VALIDATE_PLAN,
                error,
                state,
                attempt=lifetime_attempt,
            )

    @staticmethod
    def _append_failed_action(
        state: AgentState,
        action: AgentAction,
        reason: str,
        lifetime_attempt: int,
        result: ActionResult,
    ) -> ActionRecord:
        error = result.error or "工具执行失败"
        state.errors.append(f"{action.value}: {error}")
        record = ActionRecord(
            step=state.current_step,
            action=action,
            reason=reason,
            attempt=lifetime_attempt,
            success=False,
            error=error,
            tool_name=result.tool_name,
            error_type=result.error_type,
            retryable=result.retryable,
            duration_ms=result.duration_ms,
            circuit_state=result.circuit_state,
            provider_code=result.provider_code,
            provider_message=result.provider_message,
        )
        state.action_history.append(record)
        return record

    def _raise_budget_exhausted(self, state: AgentState, reason: str) -> None:
        state.mark_budget_exhausted(reason)
        self._checkpoint(state)
        raise AgentBudgetExceededError(reason, state)

    @staticmethod
    def _build_tool_payload(state: AgentState, action: AgentAction) -> dict[str, Any]:
        """只向目标工具传递其需要的数据，避免工具直接依赖整个可变状态。"""

        if action is AgentAction.SEARCH_ATTRACTIONS:
            return {
                "city": state.request.city,
                "preferences": state.request.preferences,
            }
        if action is AgentAction.SUPPLEMENT_ATTRACTIONS:
            if state.commute_supplement_query is None:
                raise ValueError("Commute supplement query is required")
            return state.commute_supplement_query.model_dump(mode="json")
        if action is AgentAction.GET_WEATHER:
            return {"city": state.request.city}
        if action is AgentAction.SEARCH_HOTELS:
            return {"city": state.request.city}
        if action is AgentAction.SEARCH_RESTAURANTS:
            if state.trip_plan is None:
                raise ValueError("Trip plan is required before restaurant search")
            return {
                "city": state.request.city,
                "keywords": "餐厅",
                "anchors": build_restaurant_search_anchors(
                    state.trip_plan,
                    max_anchors=settings.AMAP_MAX_RESTAURANT_SEARCH_ANCHORS,
                ),
                "radius_meters": settings.AMAP_RESTAURANT_SEARCH_RADIUS_METERS,
            }
        if action is AgentAction.GENERATE_PLAN:
            return {
                "session_id": state.session_id,
                "request": state.request,
                "attractions": state.attractions,
                "weather": state.weather,
                "hotels": state.hotels,
            }
        if action is AgentAction.ESTIMATE_ROUTES:
            if state.trip_plan is None:
                raise ValueError("Trip plan is required before route estimation")
            return {
                "city": state.request.city,
                "plan_fingerprint": plan_route_fingerprint(
                    state.request,
                    state.trip_plan,
                ),
                "legs": build_route_legs(
                    state.request,
                    state.trip_plan,
                    attractions=state.attractions,
                    hotels=state.hotels,
                ),
            }
        if action is AgentAction.REPAIR_PLAN:
            return {
                "request": state.request,
                "current_plan": state.trip_plan,
                "validation_result": state.last_validation_result,
                "attractions": state.attractions,
                "weather": state.weather,
                "hotels": state.hotels,
            }
        raise ValueError(f"action has no registered tool payload: {action.value}")

    def _apply_tool_result(
        self,
        state: AgentState,
        action: AgentAction,
        result: ActionResult,
    ) -> None:
        """按动作类型把工具输出写入对应状态字段，并重置需要重新校验的结果。"""

        if action is AgentAction.SEARCH_ATTRACTIONS:
            if not isinstance(result.data, dict):
                raise ValueError("景点工具结果必须是对象")
            state.attractions = result.data
            return
        if action is AgentAction.SUPPLEMENT_ATTRACTIONS:
            if state.commute_supplement_query is None:
                raise ValueError("Attraction supplement query is required")
            nearby = NearbyAttractionSearchResult.model_validate(result.data)
            query = state.commute_supplement_query
            if (
                nearby.radius_meters != query.radius_meters
                or nearby.page != query.page
                or nearby.center != query.center
            ):
                raise ValueError("Nearby candidates do not match the pending search query")
            merged = self.commute_supplementer.merge(state.attractions, nearby)
            state.attractions = merged.pool
            self._invalidate_evaluation_fingerprints(
                state, "constraints", "validation"
            )
            status = "completed" if merged.added_candidates else "empty"
            if state.content_refill_status == "supplement_needed":
                state.content_refill_supplement_search_count += 1
                state.content_refill_supplement_history.append(
                    CommuteSupplementRecord(
                        attempt=state.content_refill_supplement_search_count,
                        status=status,
                        reason=(
                            "Nearby candidates merged for minimum content refill"
                            if merged.added_candidates
                            else "Nearby search returned no new usable refill candidates"
                        ),
                        target_attraction_name=query.target_attraction_name,
                        day_index=query.day_index,
                        attraction_index=query.attraction_index,
                        anchor_names=query.anchor_names,
                        center_longitude=query.center.longitude,
                        center_latitude=query.center.latitude,
                        radius_meters=query.radius_meters,
                        received_candidates=merged.received_candidates,
                        added_candidates=merged.added_candidates,
                        final_candidates=merged.final_candidates,
                    )
                )
                state.commute_supplement_query = None
                state.content_refill_status = "not_started"
                return

            state.commute_supplement_search_count += 1
            state.commute_supplement_history.append(
                CommuteSupplementRecord(
                    attempt=state.commute_supplement_search_count,
                    status=status,
                    reason=(
                        "Nearby candidates merged into the Amap pool"
                        if merged.added_candidates
                        else "Nearby search returned no new usable candidates"
                    ),
                    target_attraction_name=query.target_attraction_name,
                    day_index=query.day_index,
                    attraction_index=query.attraction_index,
                    anchor_names=query.anchor_names,
                    center_longitude=query.center.longitude,
                    center_latitude=query.center.latitude,
                    radius_meters=query.radius_meters,
                    received_candidates=merged.received_candidates,
                    added_candidates=merged.added_candidates,
                    final_candidates=merged.final_candidates,
                )
            )
            state.commute_supplement_query = None
            state.commute_optimization_status = "not_started"
            return
        if action is AgentAction.GET_WEATHER:
            if not isinstance(result.data, dict):
                raise ValueError("天气工具结果必须是对象")
            state.weather = result.data
            return
        if action is AgentAction.SEARCH_HOTELS:
            if not isinstance(result.data, dict):
                raise ValueError("酒店工具结果必须是对象")
            state.hotels = result.data
            return
        if action is AgentAction.SEARCH_RESTAURANTS:
            if state.trip_plan is None:
                raise ValueError("Trip plan is required before saving restaurants")
            restaurants = RestaurantSearchResult.model_validate(result.data)
            state.restaurants = restaurants.model_dump(mode="json")
            state.restaurant_plan_fingerprint = restaurant_search_source_fingerprint(
                state.trip_plan,
                max_anchors=settings.AMAP_MAX_RESTAURANT_SEARCH_ANCHORS,
            )
            # 餐厅候选变化后，派生餐饮、预算和最终校验都必须重新生成。
            state.plan_consistency_fingerprint = None
            self._invalidate_evaluation_fingerprints(state, "constraints", "validation")
            return
        if action is AgentAction.GENERATE_PLAN:
            generated = GeneratePlanResult.model_validate(result.data)
            state.rag_context = generated.rag_context
            state.trip_plan = self._normalize_llm_plan(
                state,
                generated.trip_plan,
                action,
            )
            self._clear_route_analysis(
                state,
                reset_optimization_count=True,
            )
            return
        if action is AgentAction.ESTIMATE_ROUTES:
            if state.trip_plan is None:
                raise ValueError("Trip plan is required before saving route estimates")
            route_result = RouteEstimateResult.model_validate(result.data)
            expected_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
            if route_result.plan_fingerprint != expected_fingerprint:
                raise ValueError("Route estimates do not match the current trip plan")
            state.route_estimates = route_result.model_dump(mode="json")
            state.route_plan_fingerprint = route_result.plan_fingerprint
            self._invalidate_evaluation_fingerprints(
                state, "commute", "constraints", "validation"
            )
            state.route_quality_report = evaluate_route_quality(
                state.trip_plan,
                route_result,
            )
            state.route_quality_plan_fingerprint = route_result.plan_fingerprint
            state.evaluation_input_fingerprints["route_quality"] = (
                route_quality_input_fingerprint(
                    state.request, state.trip_plan, state.route_estimates
                )
            )
            if (
                state.route_optimization_status == "not_started"
                and (
                    not state.route_quality_report.optimization_recommended
                    or state.route_optimization_count
                    >= state.execution_budget.max_route_optimization_attempts
                )
            ):
                state.route_optimization_status = "skipped"
            state.schedule_quality_report = self.schedule_evaluator.evaluate(
                state.request,
                state.trip_plan,
                route_result,
            )
            state.schedule_quality_plan_fingerprint = route_result.plan_fingerprint
            state.evaluation_input_fingerprints["schedule"] = (
                schedule_input_fingerprint(
                    state.request, state.trip_plan, state.route_estimates
                )
            )
            if (
                state.schedule_optimization_status == "not_started"
                and not state.schedule_quality_report.optimization_recommended
            ):
                state.schedule_optimization_status = "skipped"
            state.last_validation_result = None
            return
        if action is AgentAction.REPAIR_PLAN:
            state.trip_plan = self._normalize_llm_plan(
                state,
                TripPlan.model_validate(result.data),
                action,
            )
            self._clear_route_analysis(
                state,
                reset_optimization_count=False,
            )
            # LLM 已经生成了新的行程结构。允许新计划重新获得一次日程优化
            # 机会，但仍由 repair_count 和单计划优化上限保证整体执行有界。
            state.schedule_optimization_count = 0
            state.repair_count += 1
            return
        raise ValueError(f"unsupported action result: {action.value}")

    @staticmethod
    def _normalize_llm_plan(
        state: AgentState,
        plan: TripPlan,
        trigger_action: AgentAction,
    ) -> TripPlan:
        """发起任何路线请求前，确定性移除后出现的重复 POI。"""

        normalized, removed = remove_duplicate_attractions(plan)
        if removed:
            state.plan_normalization_history.append(
                PlanNormalizationRecord(
                    trigger_action=trigger_action,
                    removed_attraction_names=[name for name, _ in removed],
                    removed_paths=[path for _, path in removed],
                )
            )
        return normalized

    def _checkpoint(self, state: AgentState) -> None:
        """更新时间戳，并把当前完整状态保存为可恢复检查点。"""

        # 异步任务被删除或失去租约后，必须在落盘前停止，避免旧 Worker 重建会话。
        raise_if_task_cancelled()
        state.touch()
        if self.state_store is None:
            return
        try:
            self.checkpoint_policy.save(self.state_store, state)
        except Exception as exc:
            # 持久化失败不能被伪装成业务成功；保留内存态供 API 和验收测试诊断。
            message = f"状态检查点持久化失败: {self._safe_error_message(exc)}"
            state.status = "failed"
            if not state.errors or state.errors[-1] != message:
                state.errors.append(message)
            state.touch()
            raise AgentCheckpointError(message, state) from exc

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        message = " ".join(str(exc).split()) or exc.__class__.__name__
        return message[:1000]
