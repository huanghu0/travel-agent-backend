import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.infrastructure.notifications import NoOpTaskNotificationBus
from app.infrastructure.notifications.models import TaskNotificationMessage
from app.infrastructure.redis.task_notifications import RedisTaskNotificationBus
from app.schemas.trip_schema import TripRequest
from app.task_runtime.context import TaskCancellationRequested, TaskExecutionContext
from app.task_runtime.notifying_store import NotifyingTripTaskStore
from app.task_runtime.store import SQLiteTripTaskStore


def make_request() -> TripRequest:
    return TripRequest(
        city="杭州",
        start_date="2026-08-20",
        end_date="2026-08-22",
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["自然风光"],
        free_text_input="",
    )


class RecordingNotificationBus(NoOpTaskNotificationBus):
    def __init__(self):
        super().__init__()
        self.available = []
        self.events = []
        self.cancellations = []

    def publish_task_available(self, task_id: str) -> None:
        self.available.append(task_id)
        super().publish_task_available(task_id)

    def publish_task_event(self, task_id: str, *, event_id=None, event_type=None) -> None:
        self.events.append((task_id, event_id, event_type))
        super().publish_task_event(
            task_id,
            event_id=event_id,
            event_type=event_type,
        )

    def publish_cancellation(self, task_id: str) -> None:
        self.cancellations.append(task_id)
        super().publish_cancellation(task_id)


class FakeRedisManager:
    def __init__(self, publish_result=1):
        self.config = SimpleNamespace(enabled=True)
        self.publish_result = publish_result
        self.published = []

    def execute(self, operation, *, fallback=None):
        if self.publish_result is None:
            return fallback
        client = SimpleNamespace(publish=self._publish)
        return operation(client)

    def _publish(self, channel, payload):
        self.published.append((channel, payload))
        return self.publish_result


class TaskNotificationMessageTests(unittest.TestCase):
    def test_round_trip_contains_only_minimal_versioned_fields(self):
        message = TaskNotificationMessage(
            kind="task_event",
            task_id="task-123",
            event_id=7,
            event_type="action_completed",
        )

        loaded = TaskNotificationMessage.from_json(message.to_json())

        self.assertEqual(message.kind, loaded.kind)
        self.assertEqual(message.task_id, loaded.task_id)
        self.assertEqual(7, loaded.event_id)
        self.assertEqual(1, loaded.schema_version)

    def test_invalid_task_id_and_schema_are_rejected(self):
        with self.assertRaises(ValueError):
            TaskNotificationMessage(kind="task_event", task_id="contains space")
        with self.assertRaises(ValueError):
            TaskNotificationMessage.from_json(
                '{"schema_version":2,"kind":"task_event","task_id":"task-1"}'
            )


class LocalTaskNotificationBusTests(unittest.TestCase):
    def test_worker_wait_is_woken_without_database_poll_timeout(self):
        bus = NoOpTaskNotificationBus()
        cursor = bus.worker_cursor()
        result = []

        waiter = threading.Thread(
            target=lambda: result.append(bus.wait_for_worker(cursor, 1.0))
        )
        waiter.start()
        time.sleep(0.02)
        bus.publish_task_available("task-1")
        waiter.join(timeout=1.0)

        self.assertEqual([True], result)
        self.assertEqual(1, bus.metrics_snapshot().worker_notification_wakeups)

    def test_sse_wait_timeout_keeps_database_polling_fallback(self):
        bus = NoOpTaskNotificationBus()
        cursor = bus.task_cursor("task-1")

        notified = bus.wait_for_task("task-1", cursor, 0.01)

        self.assertFalse(notified)
        self.assertEqual(1, bus.metrics_snapshot().sse_poll_timeouts)

    def test_cancellation_signal_wakes_task_and_marks_fast_path(self):
        bus = NoOpTaskNotificationBus()
        cursor = bus.task_cursor("task-1")

        bus.publish_cancellation("task-1")

        self.assertTrue(bus.is_cancel_signalled("task-1"))
        self.assertTrue(bus.wait_for_task("task-1", cursor, 0.01))


class RedisTaskNotificationBusTests(unittest.TestCase):
    def make_bus(self, manager):
        from app.infrastructure.redis.keys import RedisKeyBuilder

        return RedisTaskNotificationBus(
            client_manager=manager,
            key_builder=RedisKeyBuilder("travel-agent:test"),
        )

    def test_publish_uses_versioned_json_and_local_wake(self):
        manager = FakeRedisManager()
        bus = self.make_bus(manager)
        cursor = bus.worker_cursor()

        bus.publish_task_available("task-1")

        self.assertTrue(bus.wait_for_worker(cursor, 0.01))
        self.assertEqual(1, len(manager.published))
        message = TaskNotificationMessage.from_json(manager.published[0][1])
        self.assertEqual("task_available", message.kind)

    def test_publish_failure_does_not_lose_local_notification(self):
        manager = FakeRedisManager(publish_result=None)
        bus = self.make_bus(manager)
        cursor = bus.task_cursor("task-1")

        bus.publish_task_event("task-1", event_type="action_completed")

        self.assertTrue(bus.wait_for_task("task-1", cursor, 0.01))
        self.assertEqual(1, bus.metrics_snapshot().publish_degraded)

    def test_invalid_and_duplicate_messages_do_not_expose_duplicate_business_events(self):
        manager = FakeRedisManager()
        bus = self.make_bus(manager)
        bus._handle_message({"data": b"not-json"})
        payload = TaskNotificationMessage(
            kind="task_event",
            task_id="task-1",
            event_id=5,
        ).to_json()
        bus._handle_message({"data": payload})
        bus._handle_message({"data": payload})

        metrics = bus.metrics_snapshot()
        self.assertEqual(1, metrics.invalid_messages)
        self.assertEqual(2, metrics.received_task_events)
        # Pub/Sub 只增加唤醒修订号；SSE 是否重复展示仍由数据库 event_id 游标决定。
        self.assertGreaterEqual(bus.task_cursor("task-1"), 2)


class NotifyingTripTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.delegate = SQLiteTripTaskStore(Path(self.tempdir.name) / "tasks.db")
        self.bus = RecordingNotificationBus()
        self.store = NotifyingTripTaskStore(
            delegate=self.delegate,
            notification_bus=self.bus,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_create_notifies_only_after_new_database_task(self):
        task, reused = self.store.create_task(make_request(), idempotency_key="click")
        same, reused_again = self.store.create_task(
            make_request(), idempotency_key="click"
        )

        self.assertFalse(reused)
        self.assertTrue(reused_again)
        self.assertEqual(task.task_id, same.task_id)
        self.assertEqual([task.task_id], self.bus.available)

    def test_claim_progress_and_terminal_changes_publish_sse_wakes(self):
        task, _ = self.store.create_task(make_request(), idempotency_key="progress")
        claimed = self.store.claim_next("worker-1", lease_seconds=30)
        self.assertIsNotNone(claimed)
        self.store.record_progress(
            task.task_id,
            worker_id="worker-1",
            event_type="action_started",
            stage="search_attractions",
            stage_name="景点搜索",
            progress_percent=10,
            current_step=1,
            max_steps=24,
            message="开始搜索",
        )
        self.store.mark_succeeded(
            task.task_id,
            "worker-1",
            session_id=task.session_id,
        )

        event_types = [item[2] for item in self.bus.events]
        self.assertIn("task_started", event_types)
        self.assertIn("action_started", event_types)
        self.assertIn("task_succeeded", event_types)

    def test_cancel_commits_before_broadcasting(self):
        task, _ = self.store.create_task(make_request(), idempotency_key="cancel")

        cancelled = self.store.request_cancel(task.task_id)

        self.assertTrue(cancelled.cancel_requested)
        self.assertEqual([task.task_id], self.bus.cancellations)
        self.assertTrue(self.delegate.get_task(task.task_id).terminal)

    def test_database_failure_does_not_publish(self):
        delegate = Mock()
        delegate.create_task.side_effect = RuntimeError("database unavailable")
        bus = RecordingNotificationBus()
        store = NotifyingTripTaskStore(delegate=delegate, notification_bus=bus)

        with self.assertRaises(RuntimeError):
            store.create_task(make_request(), idempotency_key="failed")

        self.assertEqual([], bus.available)


class CancellationFastPathTests(unittest.TestCase):
    def test_redis_signal_skips_extra_cancel_query(self):
        store = Mock(unsafe=True)
        store.assert_worker_owns_task.return_value = Mock()
        bus = NoOpTaskNotificationBus()
        bus.publish_cancellation("task-1")
        context = TaskExecutionContext(
            task_id="task-1",
            worker_id="worker-1",
            store=store,
            notification_bus=bus,
        )

        with self.assertRaises(TaskCancellationRequested):
            context.check_cancelled()

        store.is_cancel_requested.assert_not_called()
        self.assertEqual(1, bus.metrics_snapshot().cancel_fast_path_hits)

    def test_missing_redis_message_falls_back_to_database_cancel_flag(self):
        store = Mock(unsafe=True)
        store.assert_worker_owns_task.return_value = Mock()
        store.is_cancel_requested.return_value = True
        bus = NoOpTaskNotificationBus()
        context = TaskExecutionContext(
            task_id="task-1",
            worker_id="worker-1",
            store=store,
            notification_bus=bus,
        )

        with self.assertRaises(TaskCancellationRequested):
            context.check_cancelled()

        store.is_cancel_requested.assert_called_once_with("task-1")
        self.assertEqual(1, bus.metrics_snapshot().mysql_poll_fallbacks)


if __name__ == "__main__":
    unittest.main()


