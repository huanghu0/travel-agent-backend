"""旅行规划确定性运行时：根据 AgentState 选动作，并通过工具白名单有界执行。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from app.agent_runtime.exceptions import (
    AgentActionError,
    AgentBudgetExceededError,
    AgentMaxStepsError,
)
from app.agent_runtime.execution_policy import CircuitBreaker, ExecutionPolicy
from app.agent_runtime.state import (
    ActionRecord,
    AgentAction,
    AgentState,
    ConstraintOptimizationRecord,
    RouteOptimizationRecord,
    ScheduleOptimizationRecord,
)
from app.constraints import (
    ConstraintEvaluator,
    DeterministicConstraintOptimizer,
    constraint_plan_fingerprint,
)
from app.providers.amap.models import RouteEstimateResult
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
from app.tools.models import ActionResult, ToolErrorType
from app.tools.registry import ToolRegistry
from app.tools.trip_registry import build_trip_tool_registry
from app.validation import TripPlanValidator


class AgentStateStore(Protocol):
    """Minimal checkpoint interface required by the orchestrator."""

    def save_state(self, state: AgentState) -> None: ...


_ACTION_REASONS = {
    AgentAction.SEARCH_ATTRACTIONS: "景点数据尚未获取",
    AgentAction.GET_WEATHER: "天气数据尚未获取",
    AgentAction.SEARCH_HOTELS: "酒店数据尚未获取",
    AgentAction.GENERATE_PLAN: "基础数据已就绪，需要生成行程",
    AgentAction.ESTIMATE_ROUTES: "行程已生成，需要查询相邻景点的真实路线",
    AgentAction.OPTIMIZE_ROUTES: "路线质量较低，需要执行有界确定性排序优化",
    AgentAction.EVALUATE_SCHEDULE: "行程路线已就绪，需要执行确定性时间轴评估",
    AgentAction.OPTIMIZE_SCHEDULE: "日程存在超时，需要执行有界确定性跨日优化",
    AgentAction.EVALUATE_CONSTRAINTS: "\u65f6\u95f4\u8f74\u5df2\u5c31\u7eea\uff0c\u9700\u8981\u8bc4\u4f30\u884c\u7a0b\u53ef\u6267\u884c\u6027\u7ea6\u675f",
    AgentAction.OPTIMIZE_CONSTRAINTS: "\u884c\u7a0b\u5b58\u5728\u53ef\u4fee\u590d\u7ea6\u675f\u51b2\u7a81\uff0c\u9700\u8981\u6267\u884c\u6709\u754c\u786e\u5b9a\u6027\u4f18\u5316",
    AgentAction.VALIDATE_PLAN: "行程已生成，需要执行确定性语义校验",
    AgentAction.REPAIR_PLAN: "行程未通过校验，需要根据结构化问题修复",
    AgentAction.FINISH: "行程已通过校验，结束执行",
}


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
        max_duration_seconds: float = 180.0,
        max_tool_calls: int = 15,
        max_llm_calls: int = 6,
        retry_base_delay_seconds: float = 0.5,
        retry_max_delay_seconds: float = 8.0,
        retry_jitter_seconds: float = 0.25,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout_seconds: float = 30.0,
        execution_policy: ExecutionPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        state_store: AgentStateStore | None = None,
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
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if max_tool_calls < 0 or max_llm_calls < 0:
            raise ValueError("call budgets cannot be negative")

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
        self.validator = validator or TripPlanValidator()
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
            route_buffer_minutes=schedule_route_buffer_minutes,
            attraction_buffer_minutes=schedule_attraction_buffer_minutes,
        )
        self.schedule_optimizer = schedule_optimizer or DeterministicScheduleOptimizer(
            evaluator=self.schedule_evaluator,
            max_candidates=schedule_optimization_max_candidates,
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
        self.max_duration_seconds = max_duration_seconds
        self.max_tool_calls = max_tool_calls
        self.max_llm_calls = max_llm_calls
        self.state_store = state_store
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

    def run(self, request: TripRequest, *, session_id: str | None = None) -> AgentState:
        """Create and execute a new persisted session."""

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
            max_duration_seconds=self.max_duration_seconds,
            max_tool_calls=self.max_tool_calls,
            max_llm_calls=self.max_llm_calls,
            session_id=session_id,
        )
        # 步骤 2：进入确定性循环；循环中的每个动作都会写入检查点。
        return self._run_state(state)

    def resume(self, state: AgentState) -> AgentState:
        """Continue a checkpoint without resetting any lifetime execution budget."""

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

        # 步骤 2：在完成或达到最大步数前持续执行“检查预算 → 决策 → 执行 → 持久化”。
        while not state.finished and state.current_step < state.max_steps:
            # 2.1 每轮先检查总执行时长，超时后禁止继续调用工具。
            runtime_reason = self.execution_policy.runtime_budget_reason(state)
            if runtime_reason:
                self._raise_budget_exhausted(state, runtime_reason)

            # 2.2 只根据当前状态选择下一动作，不让 LLM 决定流程。
            action = self.decide_next_action(state)
            key = action.value
            attempts_in_run[key] = attempts_in_run.get(key, 0) + 1
            try:
                # 2.3 执行一个原子动作；失败时由执行策略判断是否可以重试。
                self.execute_action(
                    state,
                    action,
                    attempt_in_run=attempts_in_run[key],
                )
            finally:
                # 2.4 无论成功还是异常都保存检查点，保证任务可以复盘和恢复。
                self._checkpoint(state)

        # 步骤 3：达到最大步数仍未结束时，明确失败，防止无限循环。
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

    @staticmethod
    def decide_next_action(state: AgentState) -> AgentAction:
        """Choose the next action from state only; no LLM is involved."""

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
        if (
            state.route_estimates is None
            or state.route_plan_fingerprint != current_route_fingerprint
        ):
            return AgentAction.ESTIMATE_ROUTES

        # Every current route snapshot must have a score before downstream
        # schedule evaluation. This also upgrades older SQLite checkpoints.
        if (
            state.route_quality_report is None
            or state.route_quality_plan_fingerprint != current_route_fingerprint
        ):
            return AgentAction.OPTIMIZE_ROUTES

        # Route ordering always reaches a terminal state before schedule balancing.
        if state.route_optimization_status == "candidate_pending":
            return AgentAction.OPTIMIZE_ROUTES
        if (
            state.route_optimization_status == "not_started"
            and state.route_quality_report.optimization_recommended
            and state.route_optimization_count
            < state.execution_budget.max_route_optimization_attempts
        ):
            return AgentAction.OPTIMIZE_ROUTES

        # Build a timeline locally for old checkpoints or stale plan-derived data.
        if (
            state.schedule_quality_report is None
            or state.schedule_quality_plan_fingerprint != current_route_fingerprint
        ):
            return AgentAction.EVALUATE_SCHEDULE

        # A moved attraction receives fresh real routes before acceptance.
        if state.schedule_optimization_status == "candidate_pending":
            return AgentAction.OPTIMIZE_SCHEDULE
        if (
            state.schedule_optimization_status == "not_started"
            and state.schedule_quality_report.optimization_recommended
        ):
            return AgentAction.OPTIMIZE_SCHEDULE

        current_constraint_fingerprint = constraint_plan_fingerprint(
            state.request,
            state.trip_plan,
        )
        if (
            state.constraint_report is None
            or state.constraint_plan_fingerprint != current_constraint_fingerprint
        ):
            return AgentAction.EVALUATE_CONSTRAINTS
        if state.constraint_optimization_status == "candidate_pending":
            return AgentAction.OPTIMIZE_CONSTRAINTS
        if (
            state.constraint_optimization_status == "not_started"
            and state.constraint_report.optimization_recommended
        ):
            return AgentAction.OPTIMIZE_CONSTRAINTS

        if state.last_validation_result is None:
            return AgentAction.VALIDATE_PLAN
        # 校验通过后进入终态。
        if state.last_validation_result.valid:
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
    ) -> None:
        """Execute one action and record every physical tool attempt."""

        # 步骤 1：先递增步骤和动作尝试次数，确保日志能反映真实物理调用。
        state.current_step += 1
        lifetime_attempt = state.next_attempt(action)
        current_run_attempt = attempt_in_run or lifetime_attempt
        reason = _ACTION_REASONS[action]

        # 步骤 2：FINISH 是纯状态变更，不调用外部工具。
        if action is AgentAction.FINISH:
            state.finished = True
            state.status = "completed"
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
        # Local deterministic optimization consumes neither tool nor LLM budget.
        if action is AgentAction.OPTIMIZE_ROUTES:
            self._optimize_routes(state, reason, lifetime_attempt)
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
                self.execution_policy.sleep_before_retry(decision.delay_seconds)
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
                self.execution_policy.sleep_before_retry(decision.delay_seconds)
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
        """Build the quality report for the current plan and route snapshot."""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for scoring")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        route_result = RouteEstimateResult.model_validate(state.route_estimates)
        if route_result.plan_fingerprint != current_fingerprint:
            raise ValueError("Route estimates do not match the current trip plan")
        report = evaluate_route_quality(state.trip_plan, route_result)
        state.route_quality_report = report
        state.route_quality_plan_fingerprint = current_fingerprint
        return report

    @staticmethod
    def _clear_route_analysis(
        state: AgentState,
        *,
        reset_optimization_count: bool,
    ) -> None:
        """Clear route-derived data after a plan mutation."""

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
        state.last_validation_result = None

    @staticmethod
    def _clear_constraint_analysis(
        state: AgentState,
        *,
        reset_optimization_count: bool,
    ) -> None:
        """Clear all reports and baselines derived from execution constraints."""

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
        """Propose or resolve one bounded route-order candidate."""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for optimization")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        report = state.route_quality_report
        if (
            report is None
            or state.route_quality_plan_fingerprint != current_fingerprint
        ):
            report = self._refresh_route_quality(state)

        # Second pass: compare the candidate's real Amap route quality with the
        # persisted baseline. Rejecting restores all baseline data without a call.
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
                state.schedule_optimization_status = "not_started"
                self._refresh_schedule_quality(state)

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

    def _refresh_schedule_quality(self, state: AgentState):
        """Build the current timeline report from the persisted route snapshot."""

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
        """Evaluate a stale/legacy checkpoint without invoking any external tool."""

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
        """Propose or resolve one bounded cross-day schedule candidate."""

        if state.trip_plan is None or state.route_estimates is None:
            raise ValueError("Trip plan and route estimates are required for schedule optimization")
        current_fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        report = state.schedule_quality_report
        if (
            report is None
            or state.schedule_quality_plan_fingerprint != current_fingerprint
        ):
            report = self._refresh_schedule_quality(state)

        # Verification pass after Amap routes have been fetched for the candidate.
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
        state.schedule_optimization_count += 1
        state.schedule_optimization_status = "candidate_pending"
        # Route-order optimization has already terminated. Clearing only route
        # snapshots prevents the moved plan from re-entering that earlier phase.
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
        """Evaluate current plan feasibility from persisted facts and timeline."""

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
        """Evaluate real-world execution constraints without external calls."""

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
        """Propose or verify one bounded deterministic conflict-resolution candidate."""

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
        state.constraint_optimization_count += 1
        state.constraint_optimization_status = "candidate_pending"
        # Real Amap routes and the timeline must be rebuilt before acceptance.
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
        state.validation_history.append(result)

        # 步骤 3：通过校验后，下一轮状态机会选择 FINISH。
        if result.valid:
            state.action_history.append(
                ActionRecord(
                    step=state.current_step,
                    action=AgentAction.VALIDATE_PLAN,
                    reason=reason,
                    attempt=lifetime_attempt,
                    success=True,
                    validation_error_count=0,
                    validation_warning_count=result.warning_count,
                )
            )
            return

        # 步骤 4：不通过时判断问题是否适合交给 LLM 修复，以及修复次数是否还有余额。
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
        if action is AgentAction.GET_WEATHER:
            return {"city": state.request.city}
        if action is AgentAction.SEARCH_HOTELS:
            return {"city": state.request.city}
        if action is AgentAction.GENERATE_PLAN:
            return {
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
        if action is AgentAction.GENERATE_PLAN:
            state.trip_plan = TripPlan.model_validate(result.data)
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
            state.route_quality_report = evaluate_route_quality(
                state.trip_plan,
                route_result,
            )
            state.route_quality_plan_fingerprint = route_result.plan_fingerprint
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
            if (
                state.schedule_optimization_status == "not_started"
                and not state.schedule_quality_report.optimization_recommended
            ):
                state.schedule_optimization_status = "skipped"
            state.last_validation_result = None
            return
        if action is AgentAction.REPAIR_PLAN:
            state.trip_plan = TripPlan.model_validate(result.data)
            self._clear_route_analysis(
                state,
                reset_optimization_count=False,
            )
            state.repair_count += 1
            return
        raise ValueError(f"unsupported action result: {action.value}")

    def _checkpoint(self, state: AgentState) -> None:
        """更新时间戳，并把当前完整状态保存为可恢复检查点。"""

        state.touch()
        if self.state_store is not None:
            self.state_store.save_state(state)

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        message = " ".join(str(exc).split()) or exc.__class__.__name__
        return message[:1000]
