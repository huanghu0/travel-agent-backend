"""进程内任务唤醒协调器与通知总线接口。"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class TaskNotificationMetrics:
    """通知链路的进程内指标快照，不记录任务内容。"""

    published_task_available: int = 0
    published_task_events: int = 0
    published_cancellations: int = 0
    publish_degraded: int = 0
    received_task_available: int = 0
    received_task_events: int = 0
    received_cancellations: int = 0
    invalid_messages: int = 0
    subscriber_reconnects: int = 0
    worker_notification_wakeups: int = 0
    worker_poll_timeouts: int = 0
    sse_notification_wakeups: int = 0
    sse_poll_timeouts: int = 0
    cancel_fast_path_hits: int = 0
    mysql_poll_fallbacks: int = 0

    def model_dump(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TaskNotificationHealth:
    enabled: bool
    backend: str
    subscriber_running: bool
    degraded: bool
    channels: tuple[str, ...]
    metrics: TaskNotificationMetrics

    def model_dump(self) -> dict[str, object]:
        payload = asdict(self)
        payload["metrics"] = self.metrics.model_dump()
        return payload


class TaskWakeCoordinator:
    """把 Redis 消息转换为本进程 Condition，供 Worker 和多个 SSE 连接共享。"""

    def __init__(
        self,
        *,
        max_tracked_tasks: int = 10_000,
        cancellation_ttl_seconds: float = 3600.0,
        monotonic=time.monotonic,
    ) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._worker_revision = 0
        self._task_revisions: OrderedDict[str, int] = OrderedDict()
        self._cancelled_at: OrderedDict[str, float] = OrderedDict()
        self._max_tracked_tasks = max(100, max_tracked_tasks)
        self._cancellation_ttl_seconds = max(1.0, cancellation_ttl_seconds)
        self._monotonic = monotonic

    def worker_cursor(self) -> int:
        with self._condition:
            return self._worker_revision

    def task_cursor(self, task_id: str) -> int:
        with self._condition:
            return self._task_revisions.get(task_id, 0)

    def signal_worker(self) -> None:
        with self._condition:
            self._worker_revision += 1
            self._condition.notify_all()

    def signal_task(self, task_id: str) -> None:
        with self._condition:
            revision = self._task_revisions.get(task_id, 0) + 1
            self._task_revisions[task_id] = revision
            self._task_revisions.move_to_end(task_id)
            self._trim_locked()
            self._condition.notify_all()

    def signal_cancellation(self, task_id: str) -> None:
        with self._condition:
            self._cancelled_at[task_id] = self._monotonic()
            self._cancelled_at.move_to_end(task_id)
            revision = self._task_revisions.get(task_id, 0) + 1
            self._task_revisions[task_id] = revision
            self._task_revisions.move_to_end(task_id)
            self._worker_revision += 1
            self._trim_locked()
            self._condition.notify_all()

    def wait_for_worker(self, cursor: int, timeout_seconds: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._worker_revision != cursor,
                timeout=max(0.0, timeout_seconds),
            )

    def wait_for_task(self, task_id: str, cursor: int, timeout_seconds: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: self._task_revisions.get(task_id, 0) != cursor,
                timeout=max(0.0, timeout_seconds),
            )

    def is_cancel_signalled(self, task_id: str) -> bool:
        with self._condition:
            self._prune_cancellations_locked()
            return task_id in self._cancelled_at

    def clear_cancellation(self, task_id: str) -> None:
        with self._condition:
            self._cancelled_at.pop(task_id, None)

    def _prune_cancellations_locked(self) -> None:
        cutoff = self._monotonic() - self._cancellation_ttl_seconds
        while self._cancelled_at:
            task_id, signalled_at = next(iter(self._cancelled_at.items()))
            if signalled_at >= cutoff:
                break
            self._cancelled_at.pop(task_id, None)

    def _trim_locked(self) -> None:
        self._prune_cancellations_locked()
        while len(self._task_revisions) > self._max_tracked_tasks:
            self._task_revisions.popitem(last=False)
        while len(self._cancelled_at) > self._max_tracked_tasks:
            self._cancelled_at.popitem(last=False)


@runtime_checkable
class TaskNotificationBus(Protocol):
    """可丢失通知接口；任何等待超时后都必须重新查询数据库。"""

    @property
    def enabled(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def publish_task_available(self, task_id: str) -> None: ...

    def publish_task_event(
        self,
        task_id: str,
        *,
        event_id: int | None = None,
        event_type: str | None = None,
    ) -> None: ...

    def publish_cancellation(self, task_id: str) -> None: ...

    def wake_worker_local(self) -> None: ...

    def worker_cursor(self) -> int: ...

    def task_cursor(self, task_id: str) -> int: ...

    def wait_for_worker(self, cursor: int, timeout_seconds: float) -> bool: ...

    def wait_for_task(self, task_id: str, cursor: int, timeout_seconds: float) -> bool: ...

    def is_cancel_signalled(self, task_id: str) -> bool: ...

    def clear_cancellation(self, task_id: str) -> None: ...

    def record_cancel_fast_path(self) -> None: ...

    def record_mysql_poll_fallback(self) -> None: ...

    def metrics_snapshot(self) -> TaskNotificationMetrics: ...

    def health_snapshot(self) -> TaskNotificationHealth: ...


class NoOpTaskNotificationBus:
    """Redis 关闭时仍提供进程内唤醒，并以数据库定时轮询作为跨进程兜底。"""

    def __init__(self, coordinator: TaskWakeCoordinator | None = None) -> None:
        self.coordinator = coordinator or TaskWakeCoordinator()
        self._metrics_lock = threading.Lock()
        self._metrics: dict[str, int] = {
            name: 0 for name in TaskNotificationMetrics.__dataclass_fields__
        }

    @property
    def enabled(self) -> bool:
        return False

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.coordinator.signal_worker()

    def _increment(self, name: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self._metrics[name] += amount

    def publish_task_available(self, task_id: str) -> None:
        self._increment("published_task_available")
        self.coordinator.signal_task(task_id)
        self.coordinator.signal_worker()

    def publish_task_event(
        self,
        task_id: str,
        *,
        event_id: int | None = None,
        event_type: str | None = None,
    ) -> None:
        del event_id, event_type
        self._increment("published_task_events")
        self.coordinator.signal_task(task_id)

    def publish_cancellation(self, task_id: str) -> None:
        self._increment("published_cancellations")
        self.coordinator.signal_cancellation(task_id)

    def wake_worker_local(self) -> None:
        self.coordinator.signal_worker()

    def worker_cursor(self) -> int:
        return self.coordinator.worker_cursor()

    def task_cursor(self, task_id: str) -> int:
        return self.coordinator.task_cursor(task_id)

    def wait_for_worker(self, cursor: int, timeout_seconds: float) -> bool:
        notified = self.coordinator.wait_for_worker(cursor, timeout_seconds)
        self._increment("worker_notification_wakeups" if notified else "worker_poll_timeouts")
        return notified

    def wait_for_task(self, task_id: str, cursor: int, timeout_seconds: float) -> bool:
        notified = self.coordinator.wait_for_task(task_id, cursor, timeout_seconds)
        self._increment("sse_notification_wakeups" if notified else "sse_poll_timeouts")
        return notified

    def is_cancel_signalled(self, task_id: str) -> bool:
        return self.coordinator.is_cancel_signalled(task_id)

    def clear_cancellation(self, task_id: str) -> None:
        self.coordinator.clear_cancellation(task_id)

    def record_cancel_fast_path(self) -> None:
        self._increment("cancel_fast_path_hits")

    def record_mysql_poll_fallback(self) -> None:
        self._increment("mysql_poll_fallbacks")

    def metrics_snapshot(self) -> TaskNotificationMetrics:
        with self._metrics_lock:
            return TaskNotificationMetrics(**self._metrics)

    def health_snapshot(self) -> TaskNotificationHealth:
        return TaskNotificationHealth(
            enabled=False,
            backend="local+database-polling",
            subscriber_running=False,
            degraded=False,
            channels=(),
            metrics=self.metrics_snapshot(),
        )
