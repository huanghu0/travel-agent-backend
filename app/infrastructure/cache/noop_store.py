"""Redis 关闭时的空实现，不让业务层散落 enabled 判断。"""

from __future__ import annotations

from typing import Any

from app.infrastructure.cache.metrics import CacheMetrics, CacheMetricsSnapshot
from app.infrastructure.cache.models import (
    CacheLookup,
    CacheReadStatus,
    CacheWriteResult,
    CacheWriteStatus,
)
from app.infrastructure.cache.validation import validate_cache_key


class NoOpCacheStore:
    backend_name = "noop"
    enabled = False

    def __init__(self, *, schema_version: int = 1) -> None:
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise TypeError("缓存 schema_version 必须是整数")
        if schema_version <= 0:
            raise ValueError("缓存 schema_version 必须大于 0")
        self.schema_version = schema_version
        self._metrics = CacheMetrics(self.backend_name)

    def get(self, key: str) -> CacheLookup:
        validate_cache_key(key)
        result = CacheLookup(
            status=CacheReadStatus.BYPASS,
            reason="cache_disabled",
        )
        self._metrics.record_read(result.status)
        return result

    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: int | None = None,
    ) -> CacheWriteResult:
        validate_cache_key(key)
        del value, ttl_seconds
        result = CacheWriteResult(
            status=CacheWriteStatus.SKIPPED,
            reason="cache_disabled",
        )
        self._metrics.record_write(result.status)
        return result

    def delete(self, key: str) -> bool:
        validate_cache_key(key)
        self._metrics.record_delete(deleted=False)
        return False

    def metrics_snapshot(self) -> CacheMetricsSnapshot:
        return self._metrics.snapshot()
