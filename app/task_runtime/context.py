"""Worker 执行上下文：把取消和进度能力注入同步编排循环。"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, TYPE_CHECKING

from app.task_runtime.progress import action_progress, stage_name

if TYPE_CHECKING:
    from app.agent_runtime.state import AgentState
    from app.persistence.interfaces import TripTaskStore


class TaskCancellationRequested(RuntimeError):
    """协作取消信号；必须绕过工具错误重试并由 Worker 转换为 cancelled。"""


@dataclass
class TaskExecutionContext:
    task_id: str
    worker_id: str
    store: "TripTaskStore"
    # 记录当前物理根动作开始前的历史长度，避免最后一个压缩子动作覆盖根动作结果。
    action_history_start: int = 0

    def check_ownership(self) -> None:
        """在每个安全检查点确认当前 Worker 仍持有未过期租约。"""

        self.store.assert_worker_owns_task(self.task_id, self.worker_id)

    def check_cancelled(self) -> None:
        self.check_ownership()
        if self.store.is_cancel_requested(self.task_id):
            raise TaskCancellationRequested("用户已取消旅行规划任务")

    def action_started(self, state: "AgentState", action: str) -> None:
        self.check_cancelled()
        self.action_history_start = len(state.action_history)
        name = stage_name(action)
        self.store.record_progress(
            self.task_id,
            worker_id=self.worker_id,
            event_type="action_started",
            stage=action,
            stage_name=name,
            current_action=action,
            progress_percent=action_progress(action),
            current_step=state.current_step + 1,
            max_steps=state.max_steps,
            message=f"正在执行：{name}",
            data={"action": action},
        )

    def action_completed(self, state: "AgentState", action: str) -> None:
        # 只检查本物理步骤中新产生的根动作记录。压缩子动作即使排在最后，
        # 也不能把已经成功的根动作错误显示为“等待重试”。
        matching_record = next(
            (
                record
                for record in reversed(state.action_history[self.action_history_start :])
                if record.action.value == action
            ),
            None,
        )
        success = bool(matching_record is not None and matching_record.success)
        event_type = "action_completed" if success else "action_retrying"
        name = stage_name(action)
        message = f"已完成：{name}" if success else f"{name} 未完成，等待重试或恢复"
        self.store.record_progress(
            self.task_id,
            worker_id=self.worker_id,
            event_type=event_type,
            stage=action,
            stage_name=name,
            current_action=None,
            progress_percent=action_progress(action, completed=success),
            current_step=state.current_step,
            max_steps=state.max_steps,
            message=message,
            data={
                "action": action,
                "success": success,
                "tool_calls": state.tool_call_count,
                "llm_calls": state.llm_call_count,
            },
        )
        self.check_cancelled()



_CURRENT_TASK_CONTEXT: ContextVar[TaskExecutionContext | None] = ContextVar(
    "trip_task_execution_context", default=None
)


@contextmanager
def bind_task_context(context: TaskExecutionContext) -> Iterator[None]:
    token = _CURRENT_TASK_CONTEXT.set(context)
    try:
        yield
    finally:
        _CURRENT_TASK_CONTEXT.reset(token)


def current_task_context() -> TaskExecutionContext | None:
    return _CURRENT_TASK_CONTEXT.get()


def raise_if_task_lease_lost() -> None:
    context = current_task_context()
    if context is not None:
        context.check_ownership()


def raise_if_task_cancelled() -> None:
    context = current_task_context()
    if context is not None:
        context.check_cancelled()


def notify_action_started(state: "AgentState", action: str) -> None:
    context = current_task_context()
    if context is not None:
        context.action_started(state, action)


def notify_action_completed(state: "AgentState", action: str) -> None:
    context = current_task_context()
    if context is not None:
        context.action_completed(state, action)


def sleep_with_task_cancellation(delay_seconds: float) -> None:
    """把指数退避切成短片段，使执行中取消无需等待完整退避周期。"""

    remaining = max(0.0, delay_seconds)
    while remaining > 0:
        raise_if_task_cancelled()
        interval = min(0.2, remaining)
        time.sleep(interval)
        remaining -= interval
    raise_if_task_cancelled()
