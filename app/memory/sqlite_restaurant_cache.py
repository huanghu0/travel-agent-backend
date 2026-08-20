"""使用 SQLite 持久化标准化高德餐饮搜索快照。"""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.persistence.interfaces import CacheStoreEntry, RestaurantCacheStore
from app.providers.amap.models import RestaurantSearchSnapshot


class SQLiteRestaurantCache(RestaurantCacheStore):
    """缓存稳定 POI 快照，不保存 day_index、meal_type 等具体行程字段。"""

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
                CREATE TABLE IF NOT EXISTS restaurant_cache (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    city TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_restaurant_cache_expires_at
                ON restaurant_cache(expires_at)
                """
            )

    def get(self, cache_key: str) -> RestaurantSearchSnapshot | None:
        entry = self.get_entry(cache_key)
        return entry.value if entry is not None else None

    def get_entry(
        self, cache_key: str
    ) -> CacheStoreEntry[RestaurantSearchSnapshot] | None:
        """读取 L2 条目及剩余 TTL，供 Redis L1 安全回填。"""

        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT snapshot_json, expires_at
                FROM restaurant_cache
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
                    "DELETE FROM restaurant_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                return None
            return CacheStoreEntry(
                value=RestaurantSearchSnapshot.model_validate_json(row["snapshot_json"]),
                remaining_ttl_seconds=max(
                    1, math.ceil((expires_at - now).total_seconds())
                ),
            )

    def set(
        self,
        cache_key: str,
        snapshot: RestaurantSearchSnapshot,
        *,
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            return
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO restaurant_cache (
                    cache_key,
                    provider,
                    city,
                    keywords,
                    snapshot_json,
                    created_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider = excluded.provider,
                    city = excluded.city,
                    keywords = excluded.keywords,
                    snapshot_json = excluded.snapshot_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    cache_key,
                    snapshot.provider,
                    snapshot.query_city,
                    snapshot.keywords,
                    snapshot.model_dump_json(),
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    def purge_expired(self) -> int:
        """删除全部过期餐饮快照，供维护任务或测试显式调用。"""

        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM restaurant_cache WHERE expires_at <= ?",
                (now,),
            )
            return max(0, cursor.rowcount)
