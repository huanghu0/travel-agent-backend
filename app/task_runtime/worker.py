"""基于 SQLite 持久化队列的后台 Worker。"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.agent_runtime import (
    AgentActionError,
    AgentBudgetExceededError,
    AgentConvergenceError,
    AgentMaxStepsError,
    AgentState,
    TripOrchestrator,
)
from app.memory.sqlite_store import SessionNotFoundError, SQLiteAgentStateStore
from app.task_runtime.context import (
    TaskCancellationRequested,
    TaskExecutionContext,
    bind_task_context,
    raise_if_task_cancelled,
)
from app.task_runtime.models import TaskFailureReport, TripPlanningTask
from app.task_runtime.progress import stage_name
from app.task_runtime.store import SQLiteTripTaskStore, TaskLeaseLostError


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSettings:
    poll_interval_seconds: float = 0.5
    lease_seconds: float = 30.0
    heartbeat_interval_seconds: float = 5.0
    shutdown_timeout_seconds: float = 3.0


class TripTaskWorker:
    """单进程后台 Worker；多进程部署时由 SQLite 租约保证互斥领取。"""

    def __init__(
        self,
        *,
        task_store: SQLiteTripTaskStore,
        state_store: SQLiteAgentStateStore,
        orchestrator: TripOrchestrator,
        settings: WorkerSettings | None = None,
        worker_id: str | None = None,
    ):
        self.task_store = task_store
        self.state_store = state_store
        self.orchestrator = orchestrator
        self.settings = settings or WorkerSettings()
        self.worker_id = worker_id or f"worker-{uuid4()}"
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        """幂等启动；服务重启后会继续领取 queued 或租约过期的 running 任务。"""

        with self._lock:
            if self.running:
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name=self.worker_id,
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """停止领取新任务；当前状态已由 Orchestrator 持续写入 SQLite 检查点。"""

        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
            self._wake_event.set()
        thread.join(timeout=max(0.1, self.settings.shutdown_timeout_seconds))
        with self._lock:
            if not thread.is_alive():
                self._thread = None

    def wake(self) -> None:
        self._wake_event.set()

    def run_once(self) -> bool:
        """测试和运维可调用的一次领取执行；返回是否实际领取了任务。"""

        task = self.task_store.claim_next(
            self.worker_id,
            lease_seconds=self.settings.lease_seconds,
        )
        if task is None:
            return False
        self._execute_task(task)
        return True

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                executed = self.run_once()
            except Exception:
                # 单个任务或暂时 SQLite 错误不能杀死整个 Worker；下一轮继续恢复。
                logger.exception("异步旅行规划 Worker 循环执行失败")
                executed = False
            if executed:
                continue
            self._wake_event.wait(timeout=max(0.05, self.settings.poll_interval_seconds))
            self._wake_event.clear()

    def _execute_task(self, task: TripPlanningTask) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(task.task_id, heartbeat_stop),
            name=f"{self.worker_id}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        context = TaskExecutionContext(
            task_id=task.task_id,
            worker_id=self.worker_id,
            store=self.task_store,
        )
        try:
            with bind_task_context(context):
                raise_if_task_cancelled()
                state = self._load_or_start(task)
                raise_if_task_cancelled()
                self.task_store.mark_succeeded(
                    task.task_id,
                    self.worker_id,
                    session_id=state.session_id,
                )
        except TaskCancellationRequested as exc:
            self._mark_agent_state_cancelled(task.session_id)
            self._safe_mark_cancelled(task, str(exc) or "用户已取消旅行规划任务")
        except Exception as exc:
            report, timed_out = self._build_failure_report(task, exc)
            try:
                self.task_store.mark_failed(
                    task.task_id,
                    self.worker_id,
                    report=report,
                    timed_out=timed_out,
                )
            except TaskLeaseLostError:
                # 租约已经被其他 Worker 接管时，旧 Worker 不能覆盖新执行者的状态。
                pass
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=max(0.1, self.settings.heartbeat_interval_seconds))

    def _load_or_start(self, task: TripPlanningTask) -> AgentState:
        try:
            state = self.state_store.get_state(task.session_id)
        except SessionNotFoundError:
            return self.orchestrator.run(task.request, session_id=task.session_id)
        return self.orchestrator.resume(state)

    def _heartbeat_loop(self, task_id: str, stop_event: threading.Event) -> None:
        interval = max(0.2, self.settings.heartbeat_interval_seconds)
        while not stop_event.wait(interval):
            try:
                if not self.task_store.heartbeat(
                    task_id,
                    self.worker_id,
                    lease_seconds=self.settings.lease_seconds,
                ):
                    return
            except Exception:
                # SQLite 短暂锁竞争由下一次心跳补偿，租约仍有余量。
                logger.debug("任务 %s 心跳续租失败，将在下一周期重试", task_id, exc_info=True)
                continue

    def _mark_agent_state_cancelled(self, session_id: str) -> None:
        try:
            state = self.state_store.get_state(session_id)
        except SessionNotFoundError:
            return
        state.status = "cancelled"
        self.state_store.save_state(state)

    def _safe_mark_cancelled(self, task: TripPlanningTask, message: str) -> None:
        try:
            self.task_store.mark_cancelled(task.task_id, self.worker_id, message=message)
        except TaskLeaseLostError:
            pass

    @staticmethod
    def _build_failure_report(
        task: TripPlanningTask,
        exc: Exception,
    ) -> tuple[TaskFailureReport, bool]:
        state = getattr(exc, "state", None)
        action = getattr(exc, "action", None)
        action_value = getattr(action, "value", None)
        current_step = int(getattr(state, "current_step", task.current_step) or 0)
        max_steps = int(getattr(state, "max_steps", task.max_steps) or 0)
        session_id = str(getattr(state, "session_id", task.session_id))
        last_result = getattr(state, "last_action_result", None)
        provider_code = getattr(last_result, "provider_code", None)
        provider_message = getattr(last_result, "provider_message", None)
        retryable = bool(getattr(last_result, "retryable", False))
        message = " ".join(str(exc).split()) or exc.__class__.__name__

        if isinstance(exc, AgentActionError):
            code = "agent_action_failed"
        elif isinstance(exc, AgentMaxStepsError):
            code = "agent_max_steps_reached"
        elif isinstance(exc, AgentConvergenceError):
            code = "agent_convergence_stopped"
        elif isinstance(exc, AgentBudgetExceededError):
            code = "agent_execution_budget_exceeded"
        else:
            code = "trip_task_execution_failed"

        timed_out = isinstance(exc, AgentBudgetExceededError) and any(
            marker in message.lower()
            for marker in ("duration", "deadline", "时长", "超时")
        )
        report = TaskFailureReport(
            code=code,
            message=message[:2000],
            stage=action_value or task.current_stage or "执行循环",
            stage_name=stage_name(action_value) if action_value else task.stage_name,
            action=action_value,
            retryable=retryable,
            provider_code=provider_code,
            provider_message=provider_message,
            session_id=session_id,
            current_step=current_step,
            max_steps=max_steps,
            exception_type=exc.__class__.__name__,
            details={
                "attempt": getattr(exc, "attempt", None),
                "task_attempt": task.attempt,
                "recovery_count": task.recovery_count,
            },
        )
        return report, timed_out
