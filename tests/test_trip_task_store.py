import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.schemas.trip_schema import TripRequest
from app.task_runtime.models import utc_now
from app.task_runtime.store import (
    SQLiteTripTaskStore,
    TaskIdempotencyConflictError,
    TaskLeaseLostError,
)


def request(city: str = "杭州") -> TripRequest:
    return TripRequest(
        city=city,
        start_date="2026-08-20",
        end_date="2026-08-22",
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["自然风光"],
        free_text_input="",
    )


class SQLiteTripTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteTripTaskStore(Path(self.tempdir.name) / "tasks.db")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_create_is_idempotent_and_active_request_is_deduplicated(self):
        first, reused = self.store.create_task(request(), idempotency_key="click-1")
        self.assertFalse(reused)

        same_key, reused = self.store.create_task(request(), idempotency_key="click-1")
        self.assertTrue(reused)
        self.assertEqual(first.task_id, same_key.task_id)

        # 双击意外生成了不同 key 时，活动中的相同请求仍只保留一个任务。
        same_request, reused = self.store.create_task(request(), idempotency_key="click-2")
        self.assertTrue(reused)
        self.assertEqual(first.task_id, same_request.task_id)

    def test_same_idempotency_key_cannot_be_reused_for_another_request(self):
        self.store.create_task(request(), idempotency_key="same-key")
        with self.assertRaises(TaskIdempotencyConflictError):
            self.store.create_task(request("北京"), idempotency_key="same-key")

    def test_claim_is_exclusive_and_expired_lease_can_be_recovered(self):
        created, _ = self.store.create_task(request(), idempotency_key="lease")
        first = self.store.claim_next("worker-1", lease_seconds=30)
        self.assertIsNotNone(first)
        self.assertEqual("worker-1", first.worker_id)
        self.assertIsNone(self.store.claim_next("worker-2", lease_seconds=30))

        # 模拟服务进程退出后的过期租约；新 Worker 应从同一 session 检查点恢复。
        expired = utc_now() - timedelta(seconds=1)
        with self.store._connection() as connection:
            task = self.store.get_task(created.task_id)
            task.lease_expires_at = expired
            self.store._save_task(connection, task)
        recovered = self.store.claim_next("worker-2", lease_seconds=30)
        self.assertIsNotNone(recovered)
        self.assertEqual("worker-2", recovered.worker_id)
        self.assertEqual(1, recovered.recovery_count)
        self.assertEqual(created.session_id, recovered.session_id)

    def test_expired_worker_cannot_continue_writing_progress(self):
        created, _ = self.store.create_task(request(), idempotency_key="stale-worker")
        self.store.claim_next("worker-1", lease_seconds=30)

        # 模拟旧进程暂停至租约过期，并由新 Worker 原子接管。
        expired = utc_now() - timedelta(seconds=1)
        with self.store._connection() as connection:
            task = self.store.get_task(created.task_id)
            task.lease_expires_at = expired
            self.store._save_task(connection, task)
        self.store.claim_next("worker-2", lease_seconds=30)

        with self.assertRaises(TaskLeaseLostError):
            self.store.assert_worker_owns_task(created.task_id, "worker-1")
        with self.assertRaises(TaskLeaseLostError):
            self.store.record_progress(
                created.task_id,
                worker_id="worker-1",
                event_type="action_started",
                stage="search_attractions",
                stage_name="景点搜索",
                progress_percent=4,
                current_step=1,
                max_steps=40,
                message="旧 Worker 不应继续写入",
            )

    def test_cancel_queued_task_prevents_claim(self):
        created, _ = self.store.create_task(request(), idempotency_key="cancel")
        cancelled = self.store.request_cancel(created.task_id)
        self.assertEqual("cancelled", cancelled.status)
        self.assertTrue(cancelled.cancel_requested)
        self.assertIsNone(self.store.claim_next("worker", lease_seconds=30))

    def test_events_are_replayed_strictly_after_cursor(self):
        created, _ = self.store.create_task(request(), idempotency_key="events")
        self.store.claim_next("worker", lease_seconds=30)
        events = self.store.list_events(created.task_id)
        self.assertGreaterEqual(len(events), 2)
        cursor = events[0].event_id
        replay = self.store.list_events(created.task_id, after_event_id=cursor)
        self.assertTrue(all(event.event_id > cursor for event in replay))
        self.assertEqual(len(events) - 1, len(replay))


if __name__ == "__main__":
    unittest.main()
