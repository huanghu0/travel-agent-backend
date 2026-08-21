"""Redis Pub/Sub 任务通知：只做低延迟唤醒，丢消息时回退数据库轮询。"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from redis.exceptions import RedisError

from app.infrastructure.notifications.bus import (
    NoOpTaskNotificationBus,
    TaskNotificationHealth,
    TaskWakeCoordinator,
)
from app.infrastructure.notifications.models import TaskNotificationMessage
from app.infrastructure.redis.client import RedisClientManager
from app.infrastructure.redis.keys import RedisKeyBuilder


logger = logging.getLogger(__name__)


class RedisTaskNotificationBus(NoOpTaskNotificationBus):
    """应用级单订阅线程；多个 Worker/SSE 连接共享进程内协调器。"""

    def __init__(
        self,
        *,
        client_manager: RedisClientManager,
        key_builder: RedisKeyBuilder,
        enabled: bool = True,
        reconnect_delay_seconds: float = 1.0,
        coordinator: TaskWakeCoordinator | None = None,
        message_observer: Callable[[TaskNotificationMessage], None] | None = None,
    ) -> None:
        super().__init__(coordinator=coordinator)
        self.client_manager = client_manager
        self._enabled = bool(enabled and client_manager.config.enabled)
        self.reconnect_delay_seconds = max(0.1, reconnect_delay_seconds)
        self.task_channel = key_builder.task_notification_channel()
        self.event_channel = key_builder.task_event_channel()
        self.cancellation_channel = key_builder.task_cancellation_channel()
        self._channels = (
            self.task_channel,
            self.event_channel,
            self.cancellation_channel,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._subscriber_error = False
        # 仅供压测/诊断观察端到端消息延迟；业务逻辑不能依赖该回调。
        self._message_observer = message_observer

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def subscriber_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        """幂等启动订阅器；Redis 暂时离线时线程持续重连。"""

        if not self.enabled:
            return
        with self._lifecycle_lock:
            if self.subscriber_running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._subscriber_loop,
                name="redis-task-notification-subscriber",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """停止订阅并唤醒本地等待者，避免应用退出被 Condition 阻塞。"""

        self._stop_event.set()
        self.coordinator.signal_worker()
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2.5)
        with self._lifecycle_lock:
            if thread is not None and not thread.is_alive():
                self._thread = None

    def publish_task_available(self, task_id: str) -> None:
        # 先做本地唤醒，因此即使 Redis 发布失败，本进程 Worker 也不会等待轮询超时。
        super().publish_task_available(task_id)
        self._publish(
            self.task_channel,
            TaskNotificationMessage(kind="task_available", task_id=task_id),
        )

    def publish_task_event(
        self,
        task_id: str,
        *,
        event_id: int | None = None,
        event_type: str | None = None,
    ) -> None:
        super().publish_task_event(
            task_id,
            event_id=event_id,
            event_type=event_type,
        )
        self._publish(
            self.event_channel,
            TaskNotificationMessage(
                kind="task_event",
                task_id=task_id,
                event_id=event_id,
                event_type=event_type,
            ),
        )

    def publish_cancellation(self, task_id: str) -> None:
        # 取消只有在数据库事务成功后才调用本方法，因此本地快速路径是可信信号。
        super().publish_cancellation(task_id)
        self._publish(
            self.cancellation_channel,
            TaskNotificationMessage(kind="cancellation", task_id=task_id),
        )

    def _publish(self, channel: str, message: TaskNotificationMessage) -> None:
        if not self.enabled:
            return
        result = self.client_manager.execute(
            lambda client: client.publish(channel, message.to_json()),
            fallback=None,
        )
        if result is None:
            # Redis 是非关键加速层：发布失败只计数，不向上覆盖数据库写入结果。
            self._increment("publish_degraded")

    def _subscriber_loop(self) -> None:
        first_connection = True
        while not self._stop_event.is_set():
            pubsub: Any | None = None
            try:
                client = self.client_manager.get_client(force_retry=True)
                if client is None:
                    self._subscriber_error = True
                    self._stop_event.wait(self.reconnect_delay_seconds)
                    continue
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(*self._channels)
                if not first_connection:
                    self._increment("subscriber_reconnects")
                first_connection = False
                self._subscriber_error = False
                self.client_manager.report_healthy()

                while not self._stop_event.is_set():
                    raw = pubsub.get_message(timeout=1.0)
                    if raw is None:
                        continue
                    self._handle_message(raw)
            except (RedisError, OSError, TimeoutError) as exc:
                self._subscriber_error = True
                self.client_manager.report_failure(exc)
                logger.warning(
                    "Redis 任务通知订阅中断，将回退数据库轮询并自动重连（%s）",
                    exc.__class__.__name__,
                )
                self._stop_event.wait(self.reconnect_delay_seconds)
            except Exception as exc:
                # 单条未知异常也不能杀死订阅线程；不输出可能含敏感连接信息的异常正文。
                self._subscriber_error = True
                logger.warning(
                    "Redis 任务通知订阅出现非预期异常，将自动重连（%s）",
                    exc.__class__.__name__,
                )
                self._stop_event.wait(self.reconnect_delay_seconds)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        logger.debug("关闭 Redis Pub/Sub 时发生非关键异常")

    def _handle_message(self, raw: dict[str, Any]) -> None:
        try:
            message = TaskNotificationMessage.from_json(raw.get("data", b""))
        except (TypeError, ValueError, UnicodeError):
            self._increment("invalid_messages")
            return

        if message.kind == "task_available":
            self._increment("received_task_available")
            self.coordinator.signal_task(message.task_id)
            self.coordinator.signal_worker()
        elif message.kind == "task_event":
            self._increment("received_task_events")
            self.coordinator.signal_task(message.task_id)
        elif message.kind == "cancellation":
            self._increment("received_cancellations")
            self.coordinator.signal_cancellation(message.task_id)

        observer = self._message_observer
        if observer is not None:
            try:
                observer(message)
            except Exception:
                # 观测回调不能破坏 Redis 订阅线程或业务唤醒链路。
                logger.debug("Redis 通知观测回调执行失败", exc_info=True)

    def health_snapshot(self) -> TaskNotificationHealth:
        degraded = self.enabled and (
            self._subscriber_error or (self._thread is not None and not self.subscriber_running)
        )
        return TaskNotificationHealth(
            enabled=self.enabled,
            backend="redis-pubsub+local+database-polling",
            subscriber_running=self.subscriber_running,
            degraded=degraded,
            channels=self._channels if self.enabled else (),
            metrics=self.metrics_snapshot(),
        )


def create_task_notification_bus(
    *,
    client_manager: RedisClientManager,
    enabled: bool,
    reconnect_delay_seconds: float = 1.0,
):
    """根据配置创建 Redis 或纯本地实现，调用方无需写条件分支。"""

    if not enabled or not client_manager.config.enabled:
        return NoOpTaskNotificationBus()
    return RedisTaskNotificationBus(
        client_manager=client_manager,
        key_builder=client_manager.key_builder,
        enabled=True,
        reconnect_delay_seconds=reconnect_delay_seconds,
    )
