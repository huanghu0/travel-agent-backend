"""工具执行安全策略：调用预算、指数退避，以及按工具隔离的熔断器。"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from app.agent_runtime.state import AgentState
from app.tools.models import ActionResult, ToolErrorType
from app.tools.registry import ToolRegistry


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _CircuitEntry:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    half_open_in_flight: bool = False


@dataclass(frozen=True)
class CircuitPermit:
    allowed: bool
    state: CircuitState
    retry_after_seconds: float = 0.0


class CircuitBreaker:
    """进程级线程安全熔断器；每个工具拥有互相独立的熔断状态。"""

    _TRACKED_ERRORS = {
        ToolErrorType.RATE_LIMIT,
        ToolErrorType.TIMEOUT,
        ToolErrorType.UPSTREAM,
        ToolErrorType.EXECUTION,
    }

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_timeout_seconds < 0:
            raise ValueError("recovery_timeout_seconds cannot be negative")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._clock = clock
        self._entries: dict[str, _CircuitEntry] = {}
        self._lock = threading.Lock()

    def before_call(self, tool_name: str) -> CircuitPermit:
        # 调用前状态机：CLOSED 放行；OPEN 等待恢复；HALF_OPEN 只放行一个探测请求。
        with self._lock:
            entry = self._entries.setdefault(tool_name, _CircuitEntry())
            if entry.state is CircuitState.CLOSED:
                return CircuitPermit(True, CircuitState.CLOSED)

            now = self._clock()
            if entry.state is CircuitState.OPEN:
                elapsed = now - (entry.opened_at if entry.opened_at is not None else now)
                remaining = max(0.0, self.recovery_timeout_seconds - elapsed)
                if remaining > 0:
                    return CircuitPermit(False, CircuitState.OPEN, remaining)
                entry.state = CircuitState.HALF_OPEN
                entry.half_open_in_flight = False

            if entry.half_open_in_flight:
                return CircuitPermit(
                    False,
                    CircuitState.HALF_OPEN,
                    self.recovery_timeout_seconds,
                )
            entry.half_open_in_flight = True
            return CircuitPermit(True, CircuitState.HALF_OPEN)

    def record_result(self, tool_name: str, result: ActionResult) -> CircuitState:
        # 调用后状态机：成功关闭熔断器；连续可重试失败达到阈值后打开。
        with self._lock:
            entry = self._entries.setdefault(tool_name, _CircuitEntry())
            if result.success:
                self._close(entry)
                return entry.state

            # 鉴权和参数错误不会打开熔断器，因为等待后再次调用也无法自行恢复。
            tracked = result.retryable and result.error_type in self._TRACKED_ERRORS
            if entry.state is CircuitState.HALF_OPEN:
                if tracked:
                    self._open(entry)
                else:
                    self._close(entry)
                return entry.state

            if not tracked:
                return entry.state

            entry.consecutive_failures += 1
            if entry.consecutive_failures >= self.failure_threshold:
                self._open(entry)
            return entry.state

    def state_for(self, tool_name: str) -> CircuitState:
        with self._lock:
            return self._entries.get(tool_name, _CircuitEntry()).state

    def reset(self, tool_name: str | None = None) -> None:
        with self._lock:
            if tool_name is None:
                self._entries.clear()
            else:
                self._entries.pop(tool_name, None)

    def _open(self, entry: _CircuitEntry) -> None:
        entry.state = CircuitState.OPEN
        entry.opened_at = self._clock()
        entry.half_open_in_flight = False

    @staticmethod
    def _close(entry: _CircuitEntry) -> None:
        entry.state = CircuitState.CLOSED
        entry.consecutive_failures = 0
        entry.opened_at = None
        entry.half_open_in_flight = False


@dataclass(frozen=True)
class RetryDecision:
    should_retry: bool
    delay_seconds: float = 0.0
    stop_reason: str | None = None
    budget_reason: str | None = None


class ExecutionPolicy:
    """统一管理单次工具调用边界、预算检查、熔断状态和重试决策。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        retry_base_delay_seconds: float = 0.5,
        retry_max_delay_seconds: float = 8.0,
        retry_jitter_seconds: float = 0.25,
        circuit_breaker: CircuitBreaker | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[float, float], float] = random.uniform,
        utc_clock: Callable[[], datetime] | None = None,
    ):
        if retry_base_delay_seconds < 0:
            raise ValueError("retry_base_delay_seconds cannot be negative")
        if retry_max_delay_seconds < 0:
            raise ValueError("retry_max_delay_seconds cannot be negative")
        if retry_jitter_seconds < 0:
            raise ValueError("retry_jitter_seconds cannot be negative")
        self.registry = registry
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds
        self.retry_jitter_seconds = retry_jitter_seconds
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self._sleeper = sleeper
        self._random_source = random_source
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))

    def execute_once(
        self,
        state: AgentState,
        tool_name: str,
        payload: Any,
    ) -> ActionResult:
        """完成一次物理调用；是否再次调用由 decide_retry 单独决定。"""

        # 步骤 1：读取工具声明的 LLM 成本，并在调用前检查时间/工具/LLM 三类预算。
        llm_call_cost = self.registry.llm_call_cost(tool_name)
        budget_reason = self._pre_call_budget_reason(state, llm_call_cost)
        if budget_reason:
            state.mark_budget_exhausted(budget_reason)
            return ActionResult(
                tool_name=tool_name,
                success=False,
                error=budget_reason,
                error_type=ToolErrorType.BUDGET_EXCEEDED,
                retryable=False,
            )

        # 步骤 2：检查该工具的熔断器，OPEN 状态直接快速失败，避免继续压垮上游。
        permit = self.circuit_breaker.before_call(tool_name)
        if not permit.allowed:
            retry_after = max(0, round(permit.retry_after_seconds * 1000))
            return ActionResult(
                tool_name=tool_name,
                success=False,
                error=(
                    f"工具 {tool_name} 的熔断器处于 {permit.state.value} 状态，"
                    f"请在约 {permit.retry_after_seconds:.2f} 秒后重试"
                ),
                error_type=ToolErrorType.CIRCUIT_OPEN,
                retryable=False,
                retry_after_ms=retry_after,
                circuit_state=permit.state.value,
            )

        # 步骤 3：真正调用前计数；直接高德工具的 llm_call_cost 为 0。
        state.tool_call_count += 1
        state.llm_call_count += llm_call_cost
        result = self.registry.execute(tool_name, payload)
        # 步骤 4：根据本次结果更新熔断器，并把熔断状态写回标准结果。
        circuit_state = self.circuit_breaker.record_result(tool_name, result)
        return result.model_copy(update={"circuit_state": circuit_state.value})

    def decide_retry(
        self,
        state: AgentState,
        result: ActionResult,
        *,
        attempt_in_run: int,
        max_attempts: int,
    ) -> RetryDecision:
        # 步骤 1：业务明确不可重试、达到次数上限或熔断器已打开时立即停止。
        if not result.retryable:
            return RetryDecision(False, stop_reason="result_not_retryable")
        if attempt_in_run >= max_attempts:
            return RetryDecision(False, stop_reason="attempt_limit_reached")
        if self.circuit_breaker.state_for(result.tool_name) is CircuitState.OPEN:
            return RetryDecision(False, stop_reason="circuit_open")

        # 步骤 2：即使错误可重试，也必须先确认整个会话仍有时间预算。
        runtime_reason = self.runtime_budget_reason(state)
        if runtime_reason:
            state.mark_budget_exhausted(runtime_reason)
            return RetryDecision(False, budget_reason=runtime_reason)

        # 步骤 3：计算指数退避，并增加少量随机抖动，避免并发请求同时重试。
        exponential = self.retry_base_delay_seconds * (2 ** max(0, attempt_in_run - 1))
        delay = min(self.retry_max_delay_seconds, exponential)
        if self.retry_jitter_seconds:
            delay += self._random_source(0.0, self.retry_jitter_seconds)

        # 步骤 4：如果等待结束时已经超过截止时间，则不再休眠和重试。
        now = self._utc_clock()
        if state.deadline_at is not None and now.timestamp() + delay >= state.deadline_at.timestamp():
            reason = "执行预算不足以等待下一次重试（max_duration_seconds）"
            state.mark_budget_exhausted(reason)
            return RetryDecision(False, budget_reason=reason)
        return RetryDecision(True, delay_seconds=delay)

    def sleep_before_retry(self, delay_seconds: float) -> None:
        if delay_seconds > 0:
            self._sleeper(delay_seconds)

    def runtime_budget_reason(self, state: AgentState) -> str | None:
        now = self._utc_clock()
        state.refresh_duration(now=now)
        if state.deadline_at is not None and now >= state.deadline_at:
            return (
                "智能体达到最大执行时长 "
                f"{state.execution_budget.max_duration_seconds:g} 秒"
            )
        return None

    def _pre_call_budget_reason(
        self,
        state: AgentState,
        llm_call_cost: int,
    ) -> str | None:
        runtime_reason = self.runtime_budget_reason(state)
        if runtime_reason:
            return runtime_reason
        if state.tool_call_count + 1 > state.execution_budget.max_tool_calls:
            return (
                "智能体达到最大工具调用次数 "
                f"{state.execution_budget.max_tool_calls}"
            )
        if state.llm_call_count + llm_call_cost > state.execution_budget.max_llm_calls:
            return (
                "智能体达到最大 LLM 调用次数 "
                f"{state.execution_budget.max_llm_calls}"
            )
        return None

