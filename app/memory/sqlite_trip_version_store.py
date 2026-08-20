"""行程草稿与版本的 SQLite 持久化仓储。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.persistence.exceptions import (
    DraftConflictError,
    DraftNotFoundError,
    VersionNotFoundError,
)
from app.persistence.interfaces import TripVersionStore
from app.schemas.trip_draft_schema import TripDraft, TripPlanVersion


class SQLiteTripVersionStore(TripVersionStore):
    """独立保存草稿和版本，避免编辑过程覆盖 AgentState 原检查点。"""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
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
                CREATE TABLE IF NOT EXISTS trip_plan_versions (
                    version_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_draft_id TEXT,
                    version_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    UNIQUE(session_id, version_number)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trip_drafts (
                    draft_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    draft_json TEXT NOT NULL,
                    candidate_version_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trip_versions_session ON trip_plan_versions(session_id, version_number DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_trip_drafts_session ON trip_drafts(session_id, updated_at DESC)")

    def save_version(self, version: TripPlanVersion) -> TripPlanVersion:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO trip_plan_versions
                (version_id, session_id, version_number, status, source, source_draft_id,
                 version_json, created_at, confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id) DO UPDATE SET
                  status=excluded.status, version_json=excluded.version_json,
                  confirmed_at=excluded.confirmed_at""",
                (version.version_id, version.session_id, version.version_number,
                 version.status, version.source, version.source_draft_id,
                 version.model_dump_json(), version.created_at.isoformat(),
                 version.confirmed_at.isoformat() if version.confirmed_at else None),
            )
        return version

    def get_version(self, session_id: str, version_number: int) -> TripPlanVersion:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT version_json FROM trip_plan_versions WHERE session_id=? AND version_number=?",
                (session_id, version_number),
            ).fetchone()
        if row is None:
            raise VersionNotFoundError(f"行程版本不存在：{session_id} v{version_number}")
        return TripPlanVersion.model_validate_json(row["version_json"])

    def get_version_by_id(self, version_id: str) -> TripPlanVersion:
        with self._connection() as connection:
            row = connection.execute("SELECT version_json FROM trip_plan_versions WHERE version_id=?", (version_id,)).fetchone()
        if row is None:
            raise VersionNotFoundError(f"行程版本不存在：{version_id}")
        return TripPlanVersion.model_validate_json(row["version_json"])

    def get_confirmed_version(self, session_id: str) -> TripPlanVersion | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT version_json FROM trip_plan_versions WHERE session_id=? AND status='confirmed' ORDER BY version_number DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return TripPlanVersion.model_validate_json(row["version_json"]) if row else None

    def list_versions(self, session_id: str) -> list[TripPlanVersion]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT version_json FROM trip_plan_versions WHERE session_id=? ORDER BY version_number DESC", (session_id,)
            ).fetchall()
        return [TripPlanVersion.model_validate_json(row["version_json"]) for row in rows]

    def next_version_number(self, session_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(version_number), 0) AS value FROM trip_plan_versions WHERE session_id=?", (session_id,)).fetchone()
        return int(row["value"]) + 1

    def save_draft(self, draft: TripDraft) -> TripDraft:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO trip_drafts
                (draft_id, session_id, base_version, status, draft_json, candidate_version_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(draft_id) DO UPDATE SET
                  status=excluded.status, draft_json=excluded.draft_json,
                  candidate_version_id=excluded.candidate_version_id,
                  updated_at=excluded.updated_at""",
                (draft.draft_id, draft.session_id, draft.base_version, draft.status,
                 draft.model_dump_json(), draft.candidate_version_id,
                 draft.created_at.isoformat(), draft.updated_at.isoformat()),
            )
        return draft

    def get_draft(self, session_id: str, draft_id: str) -> TripDraft:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT draft_json FROM trip_drafts WHERE session_id=? AND draft_id=?", (session_id, draft_id)
            ).fetchone()
        if row is None:
            raise DraftNotFoundError(f"行程草稿不存在：{draft_id}")
        return TripDraft.model_validate_json(row["draft_json"])

    def supersede_candidate(self, version_id: str | None) -> None:
        if not version_id:
            return
        version = self.get_version_by_id(version_id)
        if version.status == "candidate":
            version.status = "superseded"
            self.save_version(version)

    def confirm_version(self, version: TripPlanVersion) -> TripPlanVersion:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT version_json FROM trip_plan_versions WHERE session_id=? AND status='confirmed'", (version.session_id,)
            ).fetchall()
            for row in rows:
                old = TripPlanVersion.model_validate_json(row["version_json"])
                old.status = "superseded"
                connection.execute(
                    "UPDATE trip_plan_versions SET status='superseded', version_json=? WHERE version_id=?",
                    (old.model_dump_json(), old.version_id),
                )
            version.status = "confirmed"
            version.confirmed_at = now
            connection.execute(
                "UPDATE trip_plan_versions SET status='confirmed', version_json=?, confirmed_at=? WHERE version_id=?",
                (version.model_dump_json(), now.isoformat(), version.version_id),
            )
        return version
