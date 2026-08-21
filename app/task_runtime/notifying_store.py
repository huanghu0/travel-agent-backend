"""TripTaskStore 通知装饰器：数据库提交成功后再发送 best-effort 唤醒。"""

from __future__ import annotations

import logging
from typing import Any

from app.infrastructure.notifications import TaskNotificationBus
from app.persistence.interfaces import TripTaskStore
from app.schemas.trip_schema import TripRequest
from app.task_runtime.models import (
    TaskEventType,
    TaskFailureReport,
    TripPlanningTask,
    TripTaskEvent,
)


logger = logging.getLogger(__name__)


class NotifyingTripTaskStore:
    """不改变持久化语义，只在成功返回后通知 Worker、取消监听器和 SSE。"""

    def __init__(
        self,
        *,
        delegate: TripTaskStore,
        notification_bus: TaskNotificationBus,
    ) -> None:
        self.delegate = delegate
        self.notification_bus = notification_bus

    def request_fingerprint(self, request: TripRequest) -> str:
        return self.delegate.request_fingerprint(request)

    def create_task(
        self,
        request: TripRequest,
        *,
        idempotency_key: str,
    ) -> tuple[TripPlanningTask, bool]:
        task, reused = self.delegate.create_task(
            request,
            idempotency_key=idempotency_key,
        )
        if not reused:
            self._safe_notify(
                self.notification_bus.publish_task_available,
                task.task_id,
            )
        return task, reused

    def get_task(self, task_id: str) -> TripPlanningTask:
        return self.delegate.get_task(task_id)

    def list_events(
        self,
        task_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> list[TripTaskEvent]:
        return self.delegate.list_events(
            task_id,
            after_event_id=after_event_id,
            limit=limit,
        )

    def assert_worker_owns_task(
        self,
        task_id: str,
        worker_id: str,
    ) -> TripPlanningTask:
        return self.delegate.assert_worker_owns_task(task_id, worker_id)

    def record_progress(
        self,
        task_id: str,
        *,
        worker_id: str,
        event_type: TaskEventType,
        stage: str,
        stage_name: str,
        progress_percent: float,
        current_step: int,
        max_steps: int,
        message: str,
        current_action: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> TripPlanningTask:
        task = self.delegate.record_progress(
            task_id,
            worker_id=worker_id,
            event_type=event_type,
            stage=stage,
            stage_name=stage_name,
            progress_percent=progress_percent,
            current_step=current_step,
            max_steps=max_steps,
            message=message,
            current_action=current_action,
            data=data,
        )
        self._notify_event(task_id, event_type)
        return task

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> TripPlanningTask | None:
        task = self.delegate.claim_next(worker_id, lease_seconds=lease_seconds)
        if task is not None:
            event_type = "task_recovered" if task.recovery_count else "task_started"
            self._notify_event(task.task_id, event_type)
        return task

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> bool:
        return self.delegate.heartbeat(
            task_id,
            worker_id,
            lease_seconds=lease_seconds,
        )

    def is_cancel_requested(self, task_id: str) -> bool:
        return self.delegate.is_cancel_requested(task_id)

    def request_cancel(self, task_id: str) -> TripPlanningTask:
        task = self.delegate.request_cancel(task_id)
        if task.cancel_requested:
            event_type = "task_cancelled" if task.terminal else "cancellation_requested"
            self._safe_notify(self.notification_bus.publish_cancellation, task_id)
            self._notify_event(task_id, event_type)
        return task

    def mark_succeeded(
        self,
        task_id: str,
        worker_id: str,
        *,
        session_id: str,
    ) -> TripPlanningTask:
        task = self.delegate.mark_succeeded(
            task_id,
            worker_id,
            session_id=session_id,
        )
        self._notify_event(task_id, "task_succeeded")
        return task

    def mark_cancelled(
        self,
        task_id: str,
        worker_id: str,
        *,
        message: str,
    ) -> TripPlanningTask:
        task = self.delegate.mark_cancelled(
            task_id,
            worker_id,
            message=message,
        )
        self._notify_event(task_id, "task_cancelled")
        return task

    def mark_failed(
        self,
        task_id: str,
        worker_id: str,
        *,
        report: TaskFailureReport,
        timed_out: bool = False,
    ) -> TripPlanningTask:
        task = self.delegate.mark_failed(
            task_id,
            worker_id,
            report=report,
            timed_out=timed_out,
        )
        self._notify_event(task_id, "task_timed_out" if timed_out else "task_failed")
        return task

    def _notify_event(self, task_id: str, event_type: str) -> None:
        self._safe_notify(
            self.notification_bus.publish_task_event,
            task_id,
            event_type=event_type,
        )

    @staticmethod
    def _safe_notify(callback, *args, **kwargs) -> None:
        try:
            callback(*args, **kwargs)
        except Exception:
            # 通知层绝不能覆盖已经提交成功的 MySQL/SQLite 业务结果。
            logger.warning("任务通知发布失败，后续将由数据库轮询恢复", exc_info=True)
