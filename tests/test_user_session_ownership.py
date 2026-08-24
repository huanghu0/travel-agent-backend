from __future__ import annotations

import json
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, func, select

from app.agent_runtime import AgentState
from app.auth.models import UserRecord
from app.auth.store import MySQLUserStore
from app.persistence.exceptions import SessionNotFoundError, TripTaskNotFoundError
from app.persistence.mysql_agent_state_store import MySQLAgentStateStore
from app.persistence.mysql_trip_task_store import MySQLTripTaskStore
from app.persistence.sqlalchemy_models import (
    AgentSessionRow,
    Base,
    TripDraftRow,
    TripPlanVersionRow,
    TripPlanningTaskRow,
    TripTaskEventRow,
)
from app.schemas.trip_schema import TripRequest
from app.task_runtime.models import TripPlanningTask, utc_now


def request() -> TripRequest:
    return TripRequest(
        city="杭州",
        start_date="2026-08-24",
        end_date="2026-08-24",
        travel_days=1,
        transportation="步行",
        accommodation="经济型酒店",
    )


class UserSessionOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.user_store = MySQLUserStore(self.engine)
        self.state_store = MySQLAgentStateStore(self.engine)
        self.user_a = self._user("alice")
        self.user_b = self._user("bob")

    def tearDown(self) -> None:
        self.engine.dispose()

    def _user(self, username: str) -> UserRecord:
        return self.user_store.create(
            UserRecord(
                user_id=str(uuid4()),
                username=username,
                password_hash="$argon2id$test",
                created_at=utc_now(),
            )
        )

    def _state(self, user_id: str) -> AgentState:
        state = AgentState.create(request(), user_id=user_id)
        self.state_store.create_state(state)
        return state

    def test_state_reads_lists_and_analytics_are_user_scoped(self):
        state_a = self._state(self.user_a.user_id)
        self._state(self.user_b.user_id)

        loaded = self.state_store.get_state(state_a.session_id, user_id=self.user_a.user_id)
        self.assertEqual(self.user_a.user_id, loaded.user_id)
        with self.assertRaises(SessionNotFoundError):
            self.state_store.get_state(state_a.session_id, user_id=self.user_b.user_id)
        self.assertEqual(
            [state_a.session_id],
            [item.session_id for item in self.state_store.list_sessions(user_id=self.user_a.user_id)],
        )
        baseline = self.state_store.get_execution_baseline(user_id=self.user_a.user_id)
        self.assertEqual(1, baseline.matching_session_count)

    def test_task_detail_is_user_scoped(self):
        task = TripPlanningTask(
            task_id=str(uuid4()),
            session_id=str(uuid4()),
            user_id=self.user_a.user_id,
            idempotency_key="task-ownership-test",
            request_fingerprint="f" * 64,
            request=request(),
        )
        with self.engine.begin() as connection:
            connection.execute(
                TripPlanningTaskRow.__table__.insert().values(
                    task_id=task.task_id,
                    user_id=task.user_id,
                    session_id=task.session_id,
                    idempotency_key=task.idempotency_key,
                    request_fingerprint=task.request_fingerprint,
                    status=task.status,
                    cancel_requested=False,
                    task_json=task.model_dump_json(),
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )

        task_store = MySQLTripTaskStore(self.engine)
        loaded = task_store.get_task(task.task_id, user_id=self.user_a.user_id)
        self.assertEqual(self.user_a.user_id, loaded.user_id)
        with self.assertRaises(TripTaskNotFoundError):
            task_store.get_task(task.task_id, user_id=self.user_b.user_id)

    def test_delete_cascades_session_aggregate(self):
        state = self._state(self.user_a.user_id)
        task = TripPlanningTask(
            task_id=str(uuid4()),
            session_id=state.session_id,
            user_id=self.user_a.user_id,
            idempotency_key="delete-test",
            request_fingerprint="f" * 64,
            request=request(),
        )
        version_id = str(uuid4())
        draft_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                TripPlanningTaskRow.__table__.insert().values(
                    task_id=task.task_id,
                    user_id=task.user_id,
                    session_id=task.session_id,
                    idempotency_key=task.idempotency_key,
                    request_fingerprint=task.request_fingerprint,
                    status=task.status,
                    cancel_requested=False,
                    task_json=task.model_dump_json(),
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )
            connection.execute(
                TripTaskEventRow.__table__.insert().values(
                    event_id=1,
                    task_id=task.task_id,
                    event_type="task_queued",
                    event_json=json.dumps({"task_id": task.task_id}),
                    created_at=task.created_at,
                )
            )
            connection.execute(
                TripPlanVersionRow.__table__.insert().values(
                    version_id=version_id,
                    session_id=state.session_id,
                    version_number=1,
                    status="confirmed",
                    source="agent",
                    version_json="{}",
                    created_at=task.created_at,
                )
            )
            connection.execute(
                TripDraftRow.__table__.insert().values(
                    draft_id=draft_id,
                    session_id=state.session_id,
                    base_version=1,
                    status="editing",
                    draft_json="{}",
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )

        deleted_task_ids = self.state_store.delete_session(
            state.session_id,
            user_id=self.user_a.user_id,
        )
        self.assertEqual([task.task_id], deleted_task_ids)
        with self.engine.connect() as connection:
            for table in (
                AgentSessionRow.__table__,
                TripPlanningTaskRow.__table__,
                TripTaskEventRow.__table__,
                TripPlanVersionRow.__table__,
                TripDraftRow.__table__,
            ):
                self.assertEqual(
                    0,
                    connection.execute(select(func.count()).select_from(table)).scalar_one(),
                )
        with self.assertRaises(SessionNotFoundError):
            self.state_store.save_state(state)


if __name__ == "__main__":
    unittest.main()
