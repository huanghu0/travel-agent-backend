"""使用 MySQL 持久化标准化高德餐饮候选快照。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.persistence.interfaces import RestaurantCacheStore
from app.persistence.mysql_base import MySQLStoreBase, as_utc, mysql_utc, utc_now
from app.persistence.sqlalchemy_models import RestaurantCacheRow
from app.providers.amap.models import RestaurantSearchSnapshot


class MySQLRestaurantCache(MySQLStoreBase, RestaurantCacheStore):
    """保存可跨行程复用的餐饮 POI 快照，不保存具体日程字段。"""

    table = RestaurantCacheRow.__table__

    def get(self, cache_key: str) -> RestaurantSearchSnapshot | None:
        now = utc_now()
        with self.engine.begin() as connection:
            row = connection.execute(
                select(self.table.c.snapshot_json, self.table.c.expires_at).where(
                    self.table.c.cache_key == cache_key
                )
            ).mappings().one_or_none()
            if row is None:
                return None
            if as_utc(row["expires_at"]) <= now:
                connection.execute(delete(self.table).where(self.table.c.cache_key == cache_key))
                return None
            return RestaurantSearchSnapshot.model_validate_json(row["snapshot_json"])

    def set(
        self,
        cache_key: str,
        snapshot: RestaurantSearchSnapshot,
        *,
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            return
        created_at = utc_now()
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        statement = mysql_insert(self.table).values(
            cache_key=cache_key,
            provider=snapshot.provider,
            city=snapshot.query_city,
            keywords=snapshot.keywords,
            snapshot_json=snapshot.model_dump_json(),
            created_at=mysql_utc(created_at),
            expires_at=mysql_utc(expires_at),
        )
        with self.engine.begin() as connection:
            connection.execute(
                statement.on_duplicate_key_update(
                    provider=statement.inserted.provider,
                    city=statement.inserted.city,
                    keywords=statement.inserted.keywords,
                    snapshot_json=statement.inserted.snapshot_json,
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
