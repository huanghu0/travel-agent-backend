"""使用 MySQL 持久化标准化高德路线结果缓存。"""

from __future__ import annotations

import math
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.persistence.interfaces import CacheStoreEntry, RouteCacheStore
from app.persistence.mysql_base import MySQLStoreBase, as_utc, mysql_utc, utc_now
from app.persistence.sqlalchemy_models import RouteCacheRow
from app.providers.amap.models import RouteEstimate


class MySQLRouteCache(MySQLStoreBase, RouteCacheStore):
    """路线缓存与 AgentState 解耦，过期记录在读取时即时清理。"""

    table = RouteCacheRow.__table__

    def get(self, cache_key: str) -> RouteEstimate | None:
        entry = self.get_entry(cache_key)
        return entry.value if entry is not None else None

    def get_entry(
        self, cache_key: str
    ) -> CacheStoreEntry[RouteEstimate] | None:
        """读取 L2 条目及剩余 TTL，避免回填 Redis 时延长原始有效期。"""

        now = utc_now()
        with self.engine.begin() as connection:
            row = connection.execute(
                select(self.table.c.estimate_json, self.table.c.expires_at).where(
                    self.table.c.cache_key == cache_key
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            expires_at = as_utc(row["expires_at"])
            if expires_at <= now:
                connection.execute(delete(self.table).where(self.table.c.cache_key == cache_key))
                return None
            return CacheStoreEntry(
                value=RouteEstimate.model_validate_json(row["estimate_json"]),
                remaining_ttl_seconds=max(
                    1, math.ceil((expires_at - now).total_seconds())
                ),
            )

    def set(self, cache_key: str, estimate: RouteEstimate, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        created_at = utc_now()
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        stored = estimate.model_copy(update={"cache_hit": False})
        statement = mysql_insert(self.table).values(
            cache_key=cache_key,
            provider=stored.provider,
            mode=stored.mode,
            estimate_json=stored.model_dump_json(),
            created_at=mysql_utc(created_at),
            expires_at=mysql_utc(expires_at),
        )
        with self.engine.begin() as connection:
            connection.execute(
                statement.on_duplicate_key_update(
                    provider=statement.inserted.provider,
                    mode=statement.inserted.mode,
                    estimate_json=statement.inserted.estimate_json,
                    created_at=statement.inserted.created_at,
                    expires_at=statement.inserted.expires_at,
                )
            )

    def purge_expired(self) -> int:
        with self.engine.begin() as connection:
            result = connection.execute(
                delete(self.table).where(self.table.c.expires_at <= mysql_utc(utc_now()))
            )
            return max(0, result.rowcount or 0)
