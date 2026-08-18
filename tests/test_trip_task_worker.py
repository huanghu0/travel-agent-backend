import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.agent_runtime.state import ActionRecord, AgentAction, AgentState
from app.memory.sqlite_store import SQLiteAgentStateStore
from app.schemas.trip_schema import TripRequest
from app.task_runtime.context import (
    TaskCancellationRequested,
    TaskExecutionContext,
    raise_if_task_cancelled,
)
from app.task_runtime.models import utc_now
from app.task_runtime.store import SQLiteTripTaskStore
from app.task_runtime.worker import TripTaskWorker, WorkerSettings


def request() -> TripRequest:
    return TripRequest(
        city="杭州",
        start_date="2026-08-20",
        end_date="2026-08-22",
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["休闲"],
        free_text_input="",
    )


class SuccessfulOrchestrator:
    def __init__(self, state_store):
        self.state_store = state_store
        self.run_calls = 0
        self.resume_calls = 0

    def run(self, trip_request, *, session_id=None):
        self.run_calls += 1
        state = AgentState.create(trip_request, session_id=session_id)
        state.status = "completed"
        state.finished = True
        self.state_store.save_state(state)
        return state

    def resume(self, state):
        self.resume_calls += 1
        state.status = "completed"
        state.finished = True
        self.state_store.save_state(state)
        return state


class CancellingOrchestrator(SuccessfulOrchestrator):
    def __init__(self, state_store, task_store, task_id):
        super().__init__(state_store)
        self.task_store = task_store
        self.task_id = task_id
        self.external_calls = 0

    def run(self, trip_request, *, session_id=None):
        state = AgentState.create(trip_request, session_id=session_id)
        self.state_store.save_state(state)
        self.external_calls += 1
        self.task_store.request_cancel(self.task_id)
        # 第二次外部调用前的统一检查必须立即中止。
        raise_if_task_cancelled()
        self.external_calls += 1
        return state


class FailingOrchestrator(SuccessfulOrchestrator):
    def run(self, trip_request, *, session_id=None):
        state = AgentState.create(trip_request, session_id=session_id)
        self.state_store.save_state(state)
        raise RuntimeError("模拟供应商不可恢复故障")


class TripTaskWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        database = Path(self.tempdir.name) / "worker.db"
        self.task_store = SQLiteTripTaskStore(database)
        self.state_store = SQLiteAgentStateStore(database)
        self.worker_settings = WorkerSettings(
            poll_interval_seconds=0.01,
            lease_seconds=5,
            heartbeat_interval_seconds=0.05,
            shutdown_timeout_seconds=0.2,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_worker_completes_task_and_exposes_result_session(self):
        task, _ = self.task_store.create_task(request(), idempotency_key="success")
        orchestrator = SuccessfulOrchestrator(self.state_store)
        worker = TripTaskWorker(
            task_store=self.task_store,
            state_store=self.state_store,
            orchestrator=orchestrator,
            settings=self.worker_settings,
            worker_id="worker-success",
        )
        self.assertTrue(worker.run_once())
        saved = self.task_store.get_task(task.task_id)
        self.assertEqual("succeeded", saved.status)
        self.assertEqual(task.session_id, saved.result_session_id)
        self.assertEqual(1, orchestrator.run_calls)

    def test_worker_resumes_existing_agent_checkpoint(self):
        task, _ = self.task_store.create_task(request(), idempotency_key="resume")
        state = AgentState.create(request(), session_id=task.session_id)
        self.state_store.save_state(state)
        orchestrator = SuccessfulOrchestrator(self.state_store)
        worker = TripTaskWorker(
            task_store=self.task_store,
            state_store=self.state_store,
            orchestrator=orchestrator,
            settings=self.worker_settings,
            worker_id="worker-resume",
        )
        worker.run_once()
        self.assertEqual(0, orchestrator.run_calls)
        self.assertEqual(1, orchestrator.resume_calls)

    def test_new_worker_recovers_expired_task_from_agent_checkpoint(self):
        task, _ = self.task_store.create_task(request(), idempotency_key="restart-recovery")
        state = AgentState.create(request(), session_id=task.session_id)
        self.state_store.save_state(state)
        self.task_store.claim_next("worker-before-restart", lease_seconds=30)

        # 模拟原服务进程退出且租约过期；新服务中的 Worker 应恢复同一会话。
        with self.task_store._connection() as connection:
            claimed = self.task_store.get_task(task.task_id)
            claimed.lease_expires_at = utc_now() - timedelta(seconds=1)
            self.task_store._save_task(connection, claimed)

        orchestrator = SuccessfulOrchestrator(self.state_store)
        restarted_worker = TripTaskWorker(
            task_store=self.task_store,
            state_store=self.state_store,
            orchestrator=orchestrator,
            settings=self.worker_settings,
            worker_id="worker-after-restart",
        )
        self.assertTrue(restarted_worker.run_once())

        saved = self.task_store.get_task(task.task_id)
        self.assertEqual("succeeded", saved.status)
        self.assertEqual(1, saved.recovery_count)
        self.assertEqual(0, orchestrator.run_calls)
        self.assertEqual(1, orchestrator.resume_calls)

    def test_running_cancel_stops_before_next_external_call(self):
        task, _ = self.task_store.create_task(request(), idempotency_key="running-cancel")
        orchestrator = CancellingOrchestrator(
            self.state_store, self.task_store, task.task_id
        )
        worker = TripTaskWorker(
            task_store=self.task_store,
            state_store=self.state_store,
            orchestrator=orchestrator,
            settings=self.worker_settings,
            worker_id="worker-cancel",
        )
        worker.run_once()
        saved = self.task_store.get_task(task.task_id)
        self.assertEqual("cancelled", saved.status)
        self.assertEqual(1, orchestrator.external_calls)
        state = self.state_store.get_state(task.session_id)
        self.assertEqual("cancelled", state.status)

    def test_progress_uses_root_action_result_when_local_actions_are_compressed(self):
        task, _ = self.task_store.create_task(request(), idempotency_key="root-progress")
        self.task_store.claim_next("worker-progress", lease_seconds=30)
        state = AgentState.create(request(), session_id=task.session_id)
        context = TaskExecutionContext(
            task_id=task.task_id,
            worker_id="worker-progress",
            store=self.task_store,
        )

        context.action_started(state, AgentAction.ESTIMATE_ROUTES.value)
        state.action_history.append(
            ActionRecord(
                step=1,
                action=AgentAction.ESTIMATE_ROUTES,
                reason="路线查询完成",
                success=True,
            )
        )
        # 压缩子动作排在根动作之后，不能导致根动作被误报为重试。
        state.action_history.append(
            ActionRecord(
                step=1,
                action=AgentAction.EVALUATE_COMMUTE,
                reason="本地通勤评估完成",
                success=True,
                compressed=True,
                batch_root_action=AgentAction.ESTIMATE_ROUTES,
            )
        )
        state.current_step = 1
        context.action_completed(state, AgentAction.ESTIMATE_ROUTES.value)

        events = self.task_store.list_events(task.task_id)
        self.assertEqual("action_completed", events[-1].event_type)
        self.assertTrue(events[-1].data["success"])

    def test_failure_contains_structured_report(self):
        task, _ = self.task_store.create_task(request(), idempotency_key="failure")
        worker = TripTaskWorker(
            task_store=self.task_store,
            state_store=self.state_store,
            orchestrator=FailingOrchestrator(self.state_store),
            settings=self.worker_settings,
            worker_id="worker-failure",
        )
        worker.run_once()
        saved = self.task_store.get_task(task.task_id)
        self.assertEqual("failed", saved.status)
        self.assertIsNotNone(saved.failure_report)
        self.assertEqual("trip_task_execution_failed", saved.failure_report.code)
        self.assertEqual("RuntimeError", saved.failure_report.exception_type)


if __name__ == "__main__":
    unittest.main()
