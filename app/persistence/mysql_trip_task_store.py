"""使用 MySQL 实现持久化任务队列、Worker 租约和可回放事件。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Iterator
from uuid import uuid4

from sqlalchemy import Connection, and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from app.persistence.exceptions import (
    TaskIdempotencyConflictError,
    TaskLeaseLostError,
    TripTaskNotFoundError,
)
from app.persistence.interfaces import TripTaskStore
from app.persistence.mysql_base import MySQLStoreBase, mysql_utc
from app.persistence.sqlalchemy_models import TripPlanningTaskRow, TripTaskEventRow
from app.schemas.trip_schema import TripRequest
from app.task_runtime.models import (
    TaskEventType,
    TaskFailureReport,
    TripPlanningTask,
    TripTaskEvent,
    utc_now,
)


class MySQLTripTaskStore(MySQLStoreBase, TripTaskStore):
    """MySQL 任务队列实现，使用行锁、跳过锁定和租约保证多 Worker 安全。"""

    ACTIVE_STATUSES = ("queued", "running", "retrying")
    task_table = TripPlanningTaskRow.__table__
    event_table = TripTaskEventRow.__table__

    @staticmethod
    def request_fingerprint(request: TripRequest) -> str:
        payload = request.model_dump(mode="json")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _task_from_payload(payload: str) -> TripPlanningTask:
        return TripPlanningTask.model_validate_json(payload)

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: str) -> str:
        key = " ".join(idempotency_key.split())
        if not key:
            raise ValueError("Idempotency-Key 不能为空")
        if len(key) > 200:
            raise ValueError("Idempotency-Key 长度不能超过 200")
        return key

    @staticmethod
    def _lock_name(kind: str, value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"trip:{kind}:{digest}"[:64]

    @contextmanager
    def _creation_connection(self, key: str, fingerprint: str) -> Iterator[Connection]:
        """同时串行化幂等键和请求指纹，避免不同 key 的并发双击创建重复任务。"""

        lock_names = sorted(
            {
                self._lock_name("key", key),
                self._lock_name("fp", fingerprint),
            }
        )
        with self.engine.connect() as connection:
            acquired: list[str] = []
            try:
                for lock_name in lock_names:
                    result = connection.execute(
                        text("SELECT GET_LOCK(:lock_name, 10)"),
                        {"lock_name": lock_name},
                    ).scalar_one()
                    if result != 1:
                        raise TimeoutError("等待 MySQL 任务创建幂等锁超时")
                    acquired.append(lock_name)
                # GET_LOCK 是连接级锁，不会随提交释放；先结束隐式事务再开启业务事务。
                connection.commit()
                with connection.begin():
                    yield connection
            finally:
                if connection.in_transaction():
                    connection.rollback()
                for lock_name in reversed(acquired):
                    connection.execute(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": lock_name},
                    )
                if connection.in_transaction():
                    connection.commit()

    @classmethod
    def _save_task(cls, connection: Connection, task: TripPlanningTask) -> None:
        task.updated_at = utc_now()
        connection.execute(
            update(cls.task_table)
            .where(cls.task_table.c.task_id == task.task_id)
            .values(
                status=task.status,
                cancel_requested=task.cancel_requested,
                worker_id=task.worker_id,
                lease_expires_at=mysql_utc(task.lease_expires_at),
                heartbeat_at=mysql_utc(task.heartbeat_at),
                task_json=task.model_dump_json(),
                updated_at=mysql_utc(task.updated_at),
            )
        )

    @classmethod
    def _insert_event(
        cls,
        connection: Connection,
        task: TripPlanningTask,
        event_type: TaskEventType,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> int:
        created_at = utc_now()
        payload = {
            "task_id": task.task_id,
            "event_type": event_type,
            "stage": task.current_stage,
            "stage_name": task.stage_name,
            "progress_percent": task.progress_percent,
            "current_step": task.current_step,
            "message": message,
            "data": data or {},
            "created_at": created_at.isoformat(),
        }
        result = connection.execute(
            cls.event_table.insert().values(
                task_id=task.task_id,
                event_type=event_type,
                event_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                created_at=mysql_utc(created_at),
            )
        )
        return int(result.lastrowid)

    def _find_reusable_task(
        self,
        connection: Connection,
        *,
        key: str,
        fingerprint: str,
        user_id: str | None,
        lock_rows: bool,
    ) -> TripPlanningTask | None:
        key_statement = select(
            self.task_table.c.task_json,
            self.task_table.c.request_fingerprint,
        ).where(
            self.task_table.c.idempotency_key == key,
            self.task_table.c.user_id == user_id,
        )
        if lock_rows:
            key_statement = key_statement.with_for_update()
        existing = connection.execute(key_statement).mappings().one_or_none()
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise TaskIdempotencyConflictError(
                    "相同 Idempotency-Key 已用于另一份旅行规划请求"
                )
            return self._task_from_payload(existing["task_json"])

        active_statement = (
            select(self.task_table.c.task_json)
            .where(
                self.task_table.c.request_fingerprint == fingerprint,
                self.task_table.c.user_id == user_id,
                self.task_table.c.status.in_(self.ACTIVE_STATUSES),
            )
            .order_by(self.task_table.c.created_at.desc())
            .limit(1)
        )
        if lock_rows:
            active_statement = active_statement.with_for_update()
        payload = connection.execute(active_statement).scalar_one_or_none()
        return self._task_from_payload(payload) if payload is not None else None

    def create_task(
        self,
        request: TripRequest,
        *,
        idempotency_key: str,
        user_id: str | None = None,
    ) -> tuple[TripPlanningTask, bool]:
        key = self._normalize_idempotency_key(idempotency_key)
        fingerprint = self.request_fingerprint(request)
        try:
            with self._creation_connection(key, fingerprint) as connection:
                existing = self._find_reusable_task(
                    connection,
                    key=key,
                    fingerprint=fingerprint,
                    user_id=user_id,
                    lock_rows=True,
                )
                if existing is not None:
                    return existing, True

                task = TripPlanningTask(
                    task_id=str(uuid4()),
                    session_id=str(uuid4()),
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    request=request,
                    user_id=user_id,
                )
                connection.execute(
                    self.task_table.insert().values(
                        task_id=task.task_id,
                        user_id=task.user_id,
                        session_id=task.session_id,
                        idempotency_key=task.idempotency_key,
                        request_fingerprint=task.request_fingerprint,
                        status=task.status,
                        cancel_requested=False,
                        worker_id=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        task_json=task.model_dump_json(),
                        created_at=mysql_utc(task.created_at),
                        updated_at=mysql_utc(task.updated_at),
                    )
                )
                self._insert_event(connection, task, "task_queued", task.message)
                return task, False
        except IntegrityError:
            # 唯一约束是最终防线；异常回滚后重新读取，保持幂等语义。
            with self.engine.connect() as connection:
                existing = self._find_reusable_task(
                    connection,
                    key=key,
                    fingerprint=fingerprint,
                    user_id=user_id,
                    lock_rows=False,
                )
            if existing is not None:
                return existing, True
            raise

    def get_task(
        self, task_id: str, *, user_id: str | None = None
    ) -> TripPlanningTask:
        filters = [self.task_table.c.task_id == task_id]
        if user_id is not None:
            filters.append(self.task_table.c.user_id == user_id)
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(self.task_table.c.task_json).where(*filters)
            ).scalar_one_or_none()
        if payload is None:
            raise TripTaskNotFoundError(f"未找到异步旅行规划任务: {task_id}")
        return self._task_from_payload(payload)

    def list_events(
        self,
        task_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> list[TripTaskEvent]:
        self.get_task(task_id)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(self.event_table.c.event_id, self.event_table.c.event_json)
                .where(
                    self.event_table.c.task_id == task_id,
                    self.event_table.c.event_id > max(0, after_event_id),
                )
                .order_by(self.event_table.c.event_id.asc())
                .limit(max(1, min(limit, 1000)))
            ).mappings().all()
        events: list[TripTaskEvent] = []
        for row in rows:
            payload = json.loads(row["event_json"])
            payload["event_id"] = row["event_id"]
            events.append(TripTaskEvent.model_validate(payload))
        return events

    @staticmethod
    def _has_valid_lease(task: TripPlanningTask, worker_id: str) -> bool:
        return (
            task.status == "running"
            and task.worker_id == worker_id
            and task.lease_expires_at is not None
            and task.lease_expires_at > utc_now()
        )

    def assert_worker_owns_task(self, task_id: str, worker_id: str) -> TripPlanningTask:
        task = self.get_task(task_id)
        if not self._has_valid_lease(task, worker_id):
            raise TaskLeaseLostError(f"Worker {worker_id} 已失去任务 {task_id} 的有效租约")
        return task

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
        with self.engine.begin() as connection:
            payload = connection.execute(
                select(self.task_table.c.task_json)
                .where(self.task_table.c.task_id == task_id)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                raise TripTaskNotFoundError(task_id)
            task = self._task_from_payload(payload)
            if not self._has_valid_lease(task, worker_id):
                raise TaskLeaseLostError(f"Worker {worker_id} 已失去任务 {task_id} 的有效租约")
            task.current_stage = stage
            task.stage_name = stage_name
            task.current_action = current_action
            task.progress_percent = max(task.progress_percent, min(99.0, progress_percent))
            task.current_step = max(task.current_step, current_step)
            task.max_steps = max(task.max_steps, max_steps)
            task.message = message
            self._save_task(connection, task)
            self._insert_event(connection, task, event_type, message, data=data)
            return task

    def claim_next(self, worker_id: str, *, lease_seconds: float) -> TripPlanningTask | None:
        """使用 ``FOR UPDATE SKIP LOCKED`` 领取最早可执行任务。"""

        now = utc_now()
        lease_expires_at = now + timedelta(seconds=max(1.0, lease_seconds))
        with self.engine.begin() as connection:
            payload = connection.execute(
                select(self.task_table.c.task_json)
                .where(
                    self.task_table.c.cancel_requested.is_(False),
                    or_(
                        self.task_table.c.status.in_(("queued", "retrying")),
                        and_(
                            self.task_table.c.status == "running",
                            self.task_table.c.lease_expires_at.is_not(None),
                            self.task_table.c.lease_expires_at < mysql_utc(now),
                        ),
                    ),
                )
                .order_by(self.task_table.c.created_at.asc())
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if payload is None:
                return None

            task = self._task_from_payload(payload)
            recovering = task.status == "running"
            task.status = "running"
            task.worker_id = worker_id
            task.lease_expires_at = lease_expires_at
            task.heartbeat_at = now
            task.started_at = task.started_at or now
            task.attempt += 1
            if recovering:
                task.recovery_count += 1
                task.message = "检测到过期租约，正在从最近检查点恢复"
                event_type: TaskEventType = "task_recovered"
            else:
                task.message = "后台 Worker 已开始执行"
                event_type = "task_started"
            task.current_stage = "running"
            task.stage_name = "准备执行"
            task.progress_percent = max(task.progress_percent, 1.0)
            self._save_task(connection, task)
            self._insert_event(
                connection,
                task,
                event_type,
                task.message,
                data={"worker_id": worker_id, "attempt": task.attempt},
            )
            return task

    def heartbeat(self, task_id: str, worker_id: str, *, lease_seconds: float) -> bool:
        now = utc_now()
        with self.engine.begin() as connection:
            payload = connection.execute(
                select(self.task_table.c.task_json)
                .where(self.task_table.c.task_id == task_id)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                return False
            task = self._task_from_payload(payload)
            if not self._has_valid_lease(task, worker_id):
                return False
            task.heartbeat_at = now
            task.lease_expires_at = now + timedelta(seconds=max(1.0, lease_seconds))
            self._save_task(connection, task)
            return True

    def is_cancel_requested(self, task_id: str) -> bool:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.task_table.c.cancel_requested, self.task_table.c.status).where(
                    self.task_table.c.task_id == task_id
                )
            ).mappings().one_or_none()
        if row is None:
            raise TripTaskNotFoundError(task_id)
        return bool(row["cancel_requested"]) or row["status"] == "cancelled"

    def request_cancel(
        self, task_id: str, *, user_id: str | None = None
    ) -> TripPlanningTask:
        filters = [self.task_table.c.task_id == task_id]
        if user_id is not None:
            filters.append(self.task_table.c.user_id == user_id)
        with self.engine.begin() as connection:
            payload = connection.execute(
                select(self.task_table.c.task_json)
                .where(*filters)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                raise TripTaskNotFoundError(f"未找到异步旅行规划任务: {task_id}")
            task = self._task_from_payload(payload)
            if task.terminal:
                return task
            task.cancel_requested = True
            if task.status in {"queued", "retrying"}:
                task.status = "cancelled"
                task.current_stage = "cancelled"
                task.stage_name = "已取消"
                task.message = "任务在等待执行时已取消"
                task.finished_at = utc_now()
                task.worker_id = None
                task.lease_expires_at = None
                task.heartbeat_at = None
                event_type: TaskEventType = "task_cancelled"
            else:
                task.message = "已收到取消请求，正在安全停止当前调用"
                event_type = "cancellation_requested"
            self._save_task(connection, task)
            self._insert_event(connection, task, event_type, task.message)
            return task

    def mark_succeeded(self, task_id: str, worker_id: str, *, session_id: str) -> TripPlanningTask:
        return self._mark_terminal(
            task_id,
            worker_id,
            status="succeeded",
            event_type="task_succeeded",
            stage="finish",
            stage_name="完成",
            message="旅行计划生成完成",
            progress_percent=100.0,
            result_session_id=session_id,
        )

    def mark_cancelled(self, task_id: str, worker_id: str, *, message: str) -> TripPlanningTask:
        return self._mark_terminal(
            task_id,
            worker_id,
            status="cancelled",
            event_type="task_cancelled",
            stage="cancelled",
            stage_name="已取消",
            message=message,
        )

    def mark_failed(
        self,
        task_id: str,
        worker_id: str,
        *,
        report: TaskFailureReport,
        timed_out: bool = False,
    ) -> TripPlanningTask:
        return self._mark_terminal(
            task_id,
            worker_id,
            status="timed_out" if timed_out else "failed",
            event_type="task_timed_out" if timed_out else "task_failed",
            stage="timed_out" if timed_out else "failed",
            stage_name="执行超时" if timed_out else "执行失败",
            message=report.message,
            failure_report=report,
            event_data={"failure_report": report.model_dump(mode="json")},
        )

    def _mark_terminal(
        self,
        task_id: str,
        worker_id: str,
        *,
        status: str,
        event_type: TaskEventType,
        stage: str,
        stage_name: str,
        message: str,
        progress_percent: float | None = None,
        result_session_id: str | None = None,
        failure_report: TaskFailureReport | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> TripPlanningTask:
        with self.engine.begin() as connection:
            payload = connection.execute(
                select(self.task_table.c.task_json)
                .where(self.task_table.c.task_id == task_id)
                .with_for_update()
            ).scalar_one_or_none()
            if payload is None:
                raise TripTaskNotFoundError(task_id)
            task = self._task_from_payload(payload)
            if task.terminal:
                return task
            if not self._has_valid_lease(task, worker_id):
                raise TaskLeaseLostError(f"Worker {worker_id} 已失去任务 {task_id} 的有效租约")
            task.status = status  # type: ignore[assignment]
            task.current_stage = stage
            task.stage_name = stage_name
            task.message = message
            task.current_action = None
            if progress_percent is not None:
                task.progress_percent = progress_percent
            task.result_session_id = result_session_id
            task.failure_report = failure_report
            task.finished_at = utc_now()
            task.worker_id = None
            task.lease_expires_at = None
            task.heartbeat_at = None
            self._save_task(connection, task)
            self._insert_event(connection, task, event_type, message, data=event_data)
            return task
