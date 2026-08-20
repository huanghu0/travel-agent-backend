"""SQLite 持久化任务队列、租约和可回放事件。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from app.persistence.exceptions import (
    TaskIdempotencyConflictError,
    TaskLeaseLostError,
    TripTaskNotFoundError,
)
from app.persistence.interfaces import TripTaskStore
from app.schemas.trip_schema import TripRequest
from app.task_runtime.models import (
    TaskEventType,
    TaskFailureReport,
    TripPlanningTask,
    TripTaskEvent,
    utc_now,
)


class SQLiteTripTaskStore(TripTaskStore):
    """通过 SQLite WAL 提供持久化队列、幂等创建和原子任务领取。"""

    ACTIVE_STATUSES = ("queued", "running", "retrying")

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trip_planning_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT,
                    task_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trip_task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES trip_planning_tasks(task_id)
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_trip_tasks_status_created
                ON trip_planning_tasks(status, created_at)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_trip_tasks_lease
                ON trip_planning_tasks(lease_expires_at)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_trip_tasks_fingerprint
                ON trip_planning_tasks(request_fingerprint, status, created_at)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_trip_task_events_task_id
                ON trip_task_events(task_id, event_id)"""
            )

    @staticmethod
    def request_fingerprint(request: TripRequest) -> str:
        payload = request.model_dump(mode="json")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TripPlanningTask:
        return TripPlanningTask.model_validate_json(row["task_json"])

    @staticmethod
    def _save_task(connection: sqlite3.Connection, task: TripPlanningTask) -> None:
        task.updated_at = utc_now()
        connection.execute(
            """
            UPDATE trip_planning_tasks
            SET status = ?, cancel_requested = ?, worker_id = ?,
                lease_expires_at = ?, heartbeat_at = ?, task_json = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                task.status,
                int(task.cancel_requested),
                task.worker_id,
                task.lease_expires_at.isoformat() if task.lease_expires_at else None,
                task.heartbeat_at.isoformat() if task.heartbeat_at else None,
                task.model_dump_json(),
                task.updated_at.isoformat(),
                task.task_id,
            ),
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
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
        cursor = connection.execute(
            """
            INSERT INTO trip_task_events(task_id, event_type, event_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                task.task_id,
                event_type,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                created_at.isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def create_task(
        self,
        request: TripRequest,
        *,
        idempotency_key: str,
    ) -> tuple[TripPlanningTask, bool]:
        """原子创建任务；幂等键或同一活动请求命中时返回既有任务。"""

        key = " ".join(idempotency_key.split())
        if not key:
            raise ValueError("Idempotency-Key 不能为空")
        if len(key) > 200:
            raise ValueError("Idempotency-Key 长度不能超过 200")
        fingerprint = self.request_fingerprint(request)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                "SELECT task_json, request_fingerprint FROM trip_planning_tasks WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing_row is not None:
                if existing_row["request_fingerprint"] != fingerprint:
                    raise TaskIdempotencyConflictError(
                        "相同 Idempotency-Key 已用于另一份旅行规划请求"
                    )
                return self._task_from_row(existing_row), True

            # 即使客户端双击时意外生成了两个 key，也复用相同请求的活动任务。
            active_row = connection.execute(
                """
                SELECT task_json FROM trip_planning_tasks
                WHERE request_fingerprint = ? AND status IN ('queued', 'running', 'retrying')
                ORDER BY created_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
            if active_row is not None:
                return self._task_from_row(active_row), True

            task = TripPlanningTask(
                task_id=str(uuid4()),
                session_id=str(uuid4()),
                idempotency_key=key,
                request_fingerprint=fingerprint,
                request=request,
            )
            connection.execute(
                """
                INSERT INTO trip_planning_tasks(
                    task_id, session_id, idempotency_key, request_fingerprint,
                    status, cancel_requested, worker_id, lease_expires_at,
                    heartbeat_at, task_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.session_id,
                    task.idempotency_key,
                    task.request_fingerprint,
                    task.status,
                    0,
                    None,
                    None,
                    None,
                    task.model_dump_json(),
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )
            self._insert_event(connection, task, "task_queued", task.message)
            return task, False

    def get_task(self, task_id: str) -> TripPlanningTask:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_json FROM trip_planning_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise TripTaskNotFoundError(f"未找到异步旅行规划任务: {task_id}")
        return self._task_from_row(row)

    def list_events(
        self,
        task_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> list[TripTaskEvent]:
        # 先确认任务存在，使无效 task_id 返回 404 而不是空事件流。
        self.get_task(task_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_json FROM trip_task_events
                WHERE task_id = ? AND event_id > ?
                ORDER BY event_id ASC LIMIT ?
                """,
                (task_id, max(0, after_event_id), max(1, min(limit, 1000))),
            ).fetchall()
        events: list[TripTaskEvent] = []
        for row in rows:
            payload = json.loads(row["event_json"])
            payload["event_id"] = row["event_id"]
            events.append(TripTaskEvent.model_validate(payload))
        return events

    def assert_worker_owns_task(self, task_id: str, worker_id: str) -> TripPlanningTask:
        """确认任务仍由指定 Worker 执行，防止过期 Worker 继续调用供应商。"""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT task_json FROM trip_planning_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise TripTaskNotFoundError(task_id)
        task = self._task_from_row(row)
        now = utc_now()
        if (
            task.status != "running"
            or task.worker_id != worker_id
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            raise TaskLeaseLostError(
                f"Worker {worker_id} 已失去任务 {task_id} 的有效租约"
            )
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
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_json FROM trip_planning_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise TripTaskNotFoundError(task_id)
            task = self._task_from_row(row)
            if (
                task.status != "running"
                or task.worker_id != worker_id
                or task.lease_expires_at is None
                or task.lease_expires_at <= utc_now()
            ):
                raise TaskLeaseLostError(
                    f"Worker {worker_id} 已失去任务 {task_id} 的有效租约"
                )
            task.current_stage = stage
            task.stage_name = stage_name
            task.current_action = current_action
            # 重试或修复可能回到较早阶段，页面进度只允许前进。
            task.progress_percent = max(task.progress_percent, min(99.0, progress_percent))
            task.current_step = max(task.current_step, current_step)
            task.max_steps = max(task.max_steps, max_steps)
            task.message = message
            self._save_task(connection, task)
            self._insert_event(connection, task, event_type, message, data=data)
            return task

    def claim_next(self, worker_id: str, *, lease_seconds: float) -> TripPlanningTask | None:
        """使用 BEGIN IMMEDIATE 和条件 UPDATE 保证任务只被一个 Worker 领取。"""

        now = utc_now()
        lease_expires_at = now + timedelta(seconds=max(1.0, lease_seconds))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id, task_json, status FROM trip_planning_tasks
                WHERE cancel_requested = 0 AND (
                    status IN ('queued', 'retrying')
                    OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                )
                ORDER BY created_at ASC LIMIT 1
                """,
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None

            task = self._task_from_row(row)
            recovering = task.status == "running"
            cursor = connection.execute(
                """
                UPDATE trip_planning_tasks
                SET worker_id = ?, lease_expires_at = ?, heartbeat_at = ?
                WHERE task_id = ? AND cancel_requested = 0 AND (
                    status IN ('queued', 'retrying')
                    OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?)
                )
                """,
                (
                    worker_id,
                    lease_expires_at.isoformat(),
                    now.isoformat(),
                    task.task_id,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None

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
        lease_expires_at = now + timedelta(seconds=max(1.0, lease_seconds))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT task_json FROM trip_planning_tasks
                WHERE task_id = ? AND status = 'running' AND worker_id = ?""",
                (task_id, worker_id),
            ).fetchone()
            if row is None:
                return False
            task = self._task_from_row(row)
            task.heartbeat_at = now
            task.lease_expires_at = lease_expires_at
            self._save_task(connection, task)
            return True

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested, status FROM trip_planning_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise TripTaskNotFoundError(task_id)
        return bool(row["cancel_requested"]) or row["status"] == "cancelled"

    def request_cancel(self, task_id: str) -> TripPlanningTask:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_json FROM trip_planning_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise TripTaskNotFoundError(f"未找到异步旅行规划任务: {task_id}")
            task = self._task_from_row(row)
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
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_json FROM trip_planning_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise TripTaskNotFoundError(task_id)
            task = self._task_from_row(row)
            if task.terminal:
                return task
            if task.worker_id != worker_id:
                raise TaskLeaseLostError(
                    f"Worker {worker_id} 已失去任务 {task_id} 的租约"
                )
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
