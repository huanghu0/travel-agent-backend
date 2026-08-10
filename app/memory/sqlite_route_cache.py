"""使用 SQLite 持久化标准化高德路线结果的缓存。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.providers.amap.models import RouteEstimate


class SQLiteRouteCache:
    """按路线分段独立持久化，不与完整 AgentState 检查点耦合。"""

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
                CREATE TABLE IF NOT EXISTS route_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    estimate_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_cache_expires_at
                ON route_cache(expires_at)
                """
            )

    def get(self, cache_key: str) -> RouteEstimate | None:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT estimate_json, expires_at
                FROM route_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
            if row is None:
                return None

            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                connection.execute(
                    "DELETE FROM route_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                return None
            return RouteEstimate.model_validate_json(row["estimate_json"])

    def set(
        self,
        cache_key: str,
        estimate: RouteEstimate,
        *,
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            return
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        stored = estimate.model_copy(update={"cache_hit": False})
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO route_cache (
                    cache_key,
                    provider,
                    mode,
                    estimate_json,
                    created_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider = excluded.provider,
                    mode = excluded.mode,
                    estimate_json = excluded.estimate_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    cache_key,
                    stored.provider,
                    stored.mode,
                    stored.model_dump_json(),
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    def purge_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM route_cache WHERE expires_at <= ?",
                (now,),
            )
            return max(0, cursor.rowcount)
