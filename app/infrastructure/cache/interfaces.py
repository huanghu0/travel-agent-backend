"""缓存 Store 协议，业务层不依赖 Redis 客户端。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.infrastructure.cache.metrics import CacheMetricsSnapshot
from app.infrastructure.cache.models import CacheLookup, CacheWriteResult


@runtime_checkable
class CacheStore(Protocol):
    """通用 JSON 缓存接口；正式业务数据仍由持久化 Store 保存。"""

    backend_name: str
    enabled: bool
    schema_version: int

    def get(self, key: str) -> CacheLookup: ...

    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: int | None = None,
    ) -> CacheWriteResult: ...

    def delete(self, key: str) -> bool: ...

    def metrics_snapshot(self) -> CacheMetricsSnapshot: ...
