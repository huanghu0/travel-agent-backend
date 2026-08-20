"""Redis 通用缓存实现：版本化 JSON、TTL、自动降级和指标。"""

from __future__ import annotations

from typing import Any

from app.infrastructure.cache.metrics import CacheMetrics, CacheMetricsSnapshot
from app.infrastructure.cache.models import (
    CacheLookup,
    CacheReadStatus,
    CacheWriteResult,
    CacheWriteStatus,
)
from app.infrastructure.cache.serialization import (
    CacheEnvelopeSerializer,
    CacheSerializationError,
)
from app.infrastructure.cache.ttl import CacheTTLPolicy
from app.infrastructure.cache.validation import validate_cache_key
from app.infrastructure.redis.client import RedisClientManager


_DEGRADED = object()


class RedisCacheStore:
    """Redis L1 通用缓存；任何 Redis 故障都转为可观测的降级结果。"""

    backend_name = "redis"
    enabled = True

    def __init__(
        self,
        client_manager: RedisClientManager,
        *,
        serializer: CacheEnvelopeSerializer,
        ttl_policy: CacheTTLPolicy,
        delete_invalid_entries: bool = True,
    ) -> None:
        self.client_manager = client_manager
        self.serializer = serializer
        self.ttl_policy = ttl_policy
        self.delete_invalid_entries = delete_invalid_entries
        self.schema_version = serializer.schema_version
        self._metrics = CacheMetrics(self.backend_name)

    def get(self, key: str) -> CacheLookup:
        key = validate_cache_key(key)
        raw = self.client_manager.execute(
            lambda client: client.get(key),
            fallback=_DEGRADED,
        )
        if raw is _DEGRADED:
            result = CacheLookup(
                status=CacheReadStatus.DEGRADED,
                reason="redis_unavailable",
            )
            self._metrics.record_read(result.status)
            return result
        if raw is None:
            result = CacheLookup(status=CacheReadStatus.MISS)
            self._metrics.record_read(result.status)
            return result

        try:
            envelope = self.serializer.loads(raw)
        except CacheSerializationError:
            self._evict_invalid(key)
            result = CacheLookup(
                status=CacheReadStatus.MISS,
                reason="invalid_entry",
            )
            self._metrics.record_read(result.status, reason=result.reason)
            return result

        if self.serializer.is_expired(envelope):
            self._evict_invalid(key)
            result = CacheLookup(
                status=CacheReadStatus.MISS,
                reason="expired_entry",
            )
            self._metrics.record_read(result.status, reason=result.reason)
            return result

        result = CacheLookup(
            status=CacheReadStatus.HIT,
            value=envelope.payload,
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
        key = validate_cache_key(key)
        decision = self.ttl_policy.resolve(ttl_seconds)
        if not decision.cacheable or decision.seconds is None:
            result = CacheWriteResult(
                status=CacheWriteStatus.SKIPPED,
                reason=decision.reason,
            )
            self._metrics.record_write(result.status)
            return result

        try:
            encoded = self.serializer.dumps(value, ttl_seconds=decision.seconds)
        except CacheSerializationError:
            self._metrics.record_write(CacheWriteStatus.SKIPPED)
            raise

        stored = self.client_manager.execute(
            lambda client: client.set(key, encoded, ex=decision.seconds),
            fallback=_DEGRADED,
        )
        if stored is _DEGRADED or stored is not True:
            result = CacheWriteResult(
                status=CacheWriteStatus.DEGRADED,
                ttl_seconds=decision.seconds,
                reason="redis_unavailable",
            )
            self._metrics.record_write(result.status)
            return result

        result = CacheWriteResult(
            status=CacheWriteStatus.STORED,
            ttl_seconds=decision.seconds,
            reason=decision.reason,
        )
        self._metrics.record_write(result.status)
        return result

    def delete(self, key: str) -> bool:
        key = validate_cache_key(key)
        deleted = self.client_manager.execute(
            lambda client: client.delete(key),
            fallback=_DEGRADED,
        )
        if deleted is _DEGRADED:
            self._metrics.record_delete(deleted=False, degraded=True)
            return False
        was_deleted = bool(deleted)
        self._metrics.record_delete(deleted=was_deleted)
        return was_deleted

    def _evict_invalid(self, key: str) -> None:
        if not self.delete_invalid_entries:
            return
        deleted = self.client_manager.execute(
            lambda client: client.delete(key),
            fallback=_DEGRADED,
        )
        if deleted is _DEGRADED:
            self._metrics.record_delete(deleted=False, degraded=True)
        else:
            self._metrics.record_delete(deleted=bool(deleted))

    def metrics_snapshot(self) -> CacheMetricsSnapshot:
        return self._metrics.snapshot()
