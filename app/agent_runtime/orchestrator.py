"""旅行规划确定性运行时：根据 AgentState 选动作，并通过工具白名单有界执行。"""

from __future__ import annotations

from typing import Any, Protocol

from app.agent_runtime.exceptions import (
    AgentActionError,
    AgentBudgetExceededError,
    AgentMaxStepsError,
)
from app.agent_runtime.execution_policy import CircuitBreaker, ExecutionPolicy
from app.agent_runtime.state import ActionRecord, AgentAction, AgentState
from app.providers.amap.models import RouteEstimateResult
from app.routing import build_route_legs, plan_route_fingerprint
from app.schemas.trip_schema import TripPlan, TripRequest
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
        max_steps: int = 16,
        max_attempts_per_action: int = 2,
        max_repair_attempts: int = 2,
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

    @staticmethod
    def _apply_tool_result(
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
            state.route_estimates = None
            state.route_plan_fingerprint = None
            state.last_validation_result = None
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
            state.last_validation_result = None
            return
        if action is AgentAction.REPAIR_PLAN:
            state.trip_plan = TripPlan.model_validate(result.data)
            state.route_estimates = None
            state.route_plan_fingerprint = None
            state.last_validation_result = None
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
