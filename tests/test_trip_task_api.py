import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main
from app.task_runtime.store import SQLiteTripTaskStore


class DummyWorker:
    def __init__(self):
        self.wake_calls = 0

    def wake(self):
        self.wake_calls += 1

    def start(self):
        pass

    def stop(self):
        pass


class TripTaskApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_store = main.trip_task_store
        self.original_worker = main.trip_task_worker
        self.original_enabled = main.settings.TRIP_TASK_WORKER_ENABLED
        main.trip_task_store = SQLiteTripTaskStore(Path(self.tempdir.name) / "api.db")
        main.trip_task_worker = DummyWorker()
        main.settings.TRIP_TASK_WORKER_ENABLED = False
        self.client = TestClient(main.app)
        self.payload = {
            "city": "杭州",
            "start_date": "2026-08-20",
            "end_date": "2026-08-22",
            "travel_days": 3,
            "transportation": "公共交通",
            "accommodation": "经济型酒店",
            "preferences": ["自然风光"],
            "free_text_input": "",
        }

    def tearDown(self):
        self.client.close()
        main.trip_task_store = self.original_store
        main.trip_task_worker = self.original_worker
        main.settings.TRIP_TASK_WORKER_ENABLED = self.original_enabled
        self.tempdir.cleanup()

    def test_create_returns_202_and_duplicate_click_reuses_task(self):
        first = self.client.post(
            "/api/trip/tasks",
            json=self.payload,
            headers={"Idempotency-Key": "frontend-click"},
        )
        self.assertEqual(202, first.status_code)
        self.assertFalse(first.json()["reused"])

        duplicate = self.client.post(
            "/api/trip/tasks",
            json=self.payload,
            headers={"Idempotency-Key": "frontend-click"},
        )
        self.assertEqual(202, duplicate.status_code)
        self.assertTrue(duplicate.json()["reused"])
        self.assertEqual(first.json()["task_id"], duplicate.json()["task_id"])

        loaded = self.client.get(f"/api/trip/tasks/{first.json()['task_id']}")
        self.assertEqual(200, loaded.status_code)
        self.assertEqual("queued", loaded.json()["status"])

    def test_cancel_and_sse_last_event_id_do_not_replay_old_events(self):
        created = self.client.post(
            "/api/trip/tasks",
            json=self.payload,
            headers={"Idempotency-Key": "sse-task"},
        ).json()
        task_id = created["task_id"]
        cancelled = self.client.post(f"/api/trip/tasks/{task_id}/cancel")
        self.assertEqual(200, cancelled.status_code)
        self.assertEqual("cancelled", cancelled.json()["status"])

        all_events = main.trip_task_store.list_events(task_id)
        cursor = all_events[0].event_id
        response = self.client.get(
            f"/api/trip/tasks/{task_id}/events",
            headers={"Last-Event-ID": str(cursor)},
        )
        self.assertEqual(200, response.status_code)
        self.assertNotIn(f"id: {cursor}\n", response.text)
        self.assertIn("event: task_cancelled", response.text)

    def test_existing_sync_and_draft_routes_remain_registered(self):
        paths = {route.path for route in main.app.routes}
        self.assertIn("/api/trip/plan", paths)
        self.assertIn("/api/trip/sessions/{session_id}/drafts", paths)
        self.assertIn(
            "/api/trip/sessions/{session_id}/drafts/{draft_id}/evaluate", paths
        )
        self.assertIn(
            "/api/trip/sessions/{session_id}/drafts/{draft_id}/confirm", paths
        )


if __name__ == "__main__":
    unittest.main()
