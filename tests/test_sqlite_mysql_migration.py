"""SQLite -> MySQL 历史迁移的确定性、幂等、验证与回滚测试。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from sqlalchemy import create_engine, delete, select, update

from app.persistence.sqlite_backup import create_sqlite_backup
from app.persistence.sqlite_mysql_migration import (
    MigrationConflictError,
    SQLiteToMySQLMigrator,
    parse_utc_datetime,
)
from app.persistence.sqlalchemy_models import AgentSessionRow, Base


SOURCE_SCHEMA = """
CREATE TABLE agent_sessions (
    session_id TEXT PRIMARY KEY, status TEXT NOT NULL, city TEXT NOT NULL,
    current_step INTEGER NOT NULL, max_steps INTEGER NOT NULL,
    action_count INTEGER NOT NULL, state_json TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    travel_days INTEGER, transportation TEXT, completion_mode TEXT,
    quality_level TEXT, quality_score REAL, warning_count INTEGER NOT NULL,
    issue_codes_json TEXT NOT NULL, tool_call_count INTEGER NOT NULL,
    llm_call_count INTEGER NOT NULL, total_duration_ms INTEGER NOT NULL
);
CREATE TABLE route_cache (
    cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, mode TEXT NOT NULL,
    estimate_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE restaurant_cache (
    cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, city TEXT NOT NULL,
    keywords TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE trip_plan_versions (
    version_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    version_number INTEGER NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL,
    source_draft_id TEXT, version_json TEXT NOT NULL,
    created_at TEXT NOT NULL, confirmed_at TEXT
);
CREATE TABLE trip_drafts (
    draft_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    base_version INTEGER NOT NULL, status TEXT NOT NULL, draft_json TEXT NOT NULL,
    candidate_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE trip_planning_tasks (
    task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL, cancel_requested INTEGER NOT NULL,
    worker_id TEXT, lease_expires_at TEXT, heartbeat_at TEXT,
    task_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE trip_task_events (
    event_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, event_type TEXT NOT NULL,
    event_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class SQLiteMySQLMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.source_path = root / "source.db"
        self.target_path = root / "target.db"
        self.engine = create_engine(f"sqlite:///{self.target_path.as_posix()}")
        Base.metadata.create_all(self.engine)
        self.migrator = SQLiteToMySQLMigrator(self.engine)
        self._create_source()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _create_source(self) -> None:
        timestamp = "2026-08-20T08:00:00+00:00"
        with closing(sqlite3.connect(self.source_path)) as connection:
            connection.executescript(SOURCE_SCHEMA)
            connection.execute(
                "INSERT INTO agent_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "session-1", "completed", "杭州", 8, 24, 8,
                    json.dumps({"session_id": "session-1"}), timestamp, timestamp,
                    1, "公共交通", "full", "good", 90.0, 0, "[]", 7, 1, 1200,
                ),
            )
            connection.execute(
                "INSERT INTO route_cache VALUES (?,?,?,?,?,?)",
                ("route-1", "amap", "transit", "{}", timestamp, "2026-08-21T08:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO restaurant_cache VALUES (?,?,?,?,?,?,?)",
                ("restaurant-1", "amap", "杭州", "餐厅", "{}", timestamp, "2026-08-21T08:00:00+00:00"),
            )
            connection.execute(
                "INSERT INTO trip_plan_versions VALUES (?,?,?,?,?,?,?,?,?)",
                ("version-1", "session-1", 1, "confirmed", "generated", None, "{}", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO trip_drafts VALUES (?,?,?,?,?,?,?,?)",
                ("draft-1", "session-1", 1, "editing", "{}", None, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO trip_planning_tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "task-1", "task-session-1", "idem-1", "f" * 64, "succeeded", 0,
                    None, None, None, "{}", timestamp, timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO trip_task_events VALUES (?,?,?,?,?)",
                (1, "task-1", "task_succeeded", "{}", timestamp),
            )
            connection.commit()

    def test_full_execute_verify_and_rollback(self):
        dry_run = self.migrator.dry_run(self.source_path)
        self.assertTrue(dry_run["valid"])
        self.assertEqual(dry_run["totals"]["source"], 7)
        self.assertEqual(dry_run["totals"]["missing"], 7)

        execution = self.migrator.execute(self.source_path)
        self.assertEqual(execution["status"], "completed")
        self.assertEqual(execution["totals"]["inserted"], 7)

        verification = self.migrator.verify(
            self.source_path,
            batch_id=execution["batch_id"],
        )
        self.assertTrue(verification["verified"])
        self.assertTrue(verification["safe_to_cutover"])
        self.assertEqual(verification["totals"]["matched"], 7)

        rollback = self.migrator.rollback(execution["batch_id"])
        self.assertTrue(rollback["fully_rolled_back"])
        self.assertEqual(rollback["totals"]["deleted"], 7)

    def test_execute_is_idempotent_and_does_not_claim_existing_rows(self):
        first = self.migrator.execute(self.source_path)
        second = self.migrator.execute(self.source_path)

        self.assertEqual(first["totals"]["inserted"], 7)
        self.assertEqual(second["totals"]["inserted"], 0)
        self.assertEqual(second["totals"]["existing_same"], 7)
        rollback = self.migrator.rollback(second["batch_id"])
        self.assertEqual(rollback["totals"]["tracked"], 0)
        self.assertEqual(rollback["totals"]["deleted"], 0)

    def test_rollback_protects_rows_modified_after_migration(self):
        execution = self.migrator.execute(self.source_path)
        with self.engine.begin() as connection:
            connection.execute(
                update(AgentSessionRow)
                .where(AgentSessionRow.session_id == "session-1")
                .values(city="上海")
            )

        rollback = self.migrator.rollback(execution["batch_id"])

        self.assertFalse(rollback["fully_rolled_back"])
        self.assertEqual(rollback["totals"]["protected_modified"], 1)
        with self.engine.connect() as connection:
            city = connection.execute(
                select(AgentSessionRow.city).where(AgentSessionRow.session_id == "session-1")
            ).scalar_one()
        self.assertEqual(city, "上海")

    def test_conflict_fails_without_overwriting_mysql(self):
        with self.engine.begin() as connection:
            connection.execute(
                AgentSessionRow.__table__.insert().values(
                    session_id="session-1", status="completed", city="上海",
                    current_step=8, max_steps=24, action_count=8,
                    state_json='{"session_id":"session-1"}',
                    created_at=parse_utc_datetime("2026-08-20T08:00:00+00:00"),
                    updated_at=parse_utc_datetime("2026-08-20T08:00:00+00:00"),
                    travel_days=1, transportation="公共交通", completion_mode="full",
                    quality_level="good", quality_score=90.0, warning_count=0,
                    issue_codes_json="[]", tool_call_count=7, llm_call_count=1,
                    total_duration_ms=1200,
                )
            )

        with self.assertRaises(MigrationConflictError):
            self.migrator.execute(self.source_path)

        with self.engine.connect() as connection:
            city = connection.execute(
                select(AgentSessionRow.city).where(AgentSessionRow.session_id == "session-1")
            ).scalar_one()
        self.assertEqual(city, "上海")

    def test_failed_batch_can_resume_from_row_credentials(self):
        with self.engine.begin() as connection:
            connection.execute(
                AgentSessionRow.__table__.insert().values(
                    session_id="session-1", status="completed", city="冲突城市",
                    current_step=8, max_steps=24, action_count=8,
                    state_json='{"session_id":"session-1"}',
                    created_at=parse_utc_datetime("2026-08-20T08:00:00+00:00"),
                    updated_at=parse_utc_datetime("2026-08-20T08:00:00+00:00"),
                    travel_days=1, transportation="公共交通", completion_mode="full",
                    quality_level="good", quality_score=90.0, warning_count=0,
                    issue_codes_json="[]", tool_call_count=7, llm_call_count=1,
                    total_duration_ms=1200,
                )
            )

        with self.assertRaises(MigrationConflictError) as caught:
            self.migrator.execute(self.source_path)
        batch_id = caught.exception.batch_id

        # 模拟人工解决冲突：删除未被该批次认领的目标行，然后恢复原批次。
        with self.engine.begin() as connection:
            connection.execute(
                delete(AgentSessionRow).where(AgentSessionRow.session_id == "session-1")
            )
        resumed = self.migrator.execute(
            self.source_path,
            resume_batch_id=batch_id,
        )

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["totals"]["inserted"], 1)
        self.assertEqual(resumed["totals"]["resumed"], 6)
        self.assertTrue(
            self.migrator.verify(self.source_path, batch_id=batch_id)["verified"]
        )

    def test_invalid_json_is_reported_by_dry_run(self):
        with closing(sqlite3.connect(self.source_path)) as connection:
            connection.execute(
                "UPDATE agent_sessions SET state_json='not-json' WHERE session_id='session-1'"
            )
            connection.commit()

        result = self.migrator.dry_run(self.source_path)

        self.assertFalse(result["valid"])
        self.assertEqual(result["tables"]["agent_sessions"]["invalid"], 1)
        self.assertIn("无效 JSON", result["errors"][0]["message"])

    def test_online_backup_creates_integrity_manifest(self):
        backup, manifest_path = create_sqlite_backup(
            self.source_path,
            Path(self.temp_dir.name) / "backups",
        )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(backup.exists())
        self.assertEqual(manifest["backup_integrity"], "ok")
        self.assertEqual(len(manifest["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
