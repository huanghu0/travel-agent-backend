"""高德领域分层缓存：Redis L1 → 数据库 L2 → Provider。

Redis 仅保存可重建的热数据；MySQL/SQLite L2 仍是缓存事实来源。
任一缓存层异常都只触发降级，不得阻断高德 Provider 主流程。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.infrastructure.cache import (
    CacheReadStatus,
    CacheStore,
    CacheWriteStatus,
)
from app.persistence.interfaces import (
    CacheStoreEntry,
    RestaurantCacheStore,
    RouteCacheStore,
)
from app.providers.amap.models import RestaurantSearchSnapshot, RouteEstimate


logger = logging.getLogger(__name__)
TValue = TypeVar("TValue", bound=BaseModel)


class LayeredCacheSource(str, Enum):
    """一次领域缓存查询最终由哪一层提供结果。"""

    L1 = "l1"
    L2 = "l2"
    MISS = "miss"


@dataclass(frozen=True, slots=True)
class LayeredCacheLookup(Generic[TValue]):
    """返回缓存值、来源和两层读取状态，供 Provider 生成请求级指标。"""

    source: LayeredCacheSource
    value: TValue | None = None
    l1_status: str = "skipped"
    l2_status: str = "skipped"

    @property
    def hit(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class LayeredCacheMetricsSnapshot:
    domain: str
    l1_reads: int
    l1_hits: int
    l1_misses: int
    l1_bypasses: int
    l1_degraded: int
    l1_invalid_payloads: int
    l1_backfills: int
    l1_writes: int
    l1_write_degraded: int
    l2_reads: int
    l2_hits: int
    l2_misses: int
    l2_errors: int
    l2_writes: int
    l2_write_errors: int
    provider_calls: int
    provider_calls_avoided_by_l1: int
    provider_calls_avoided_by_l2: int
    l1_hit_rate: float
    l2_hit_rate: float

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class _LayeredCacheMetrics:
    """线程安全地累计路线或餐饮领域的分层缓存指标。"""

    _FIELDS = (
        "l1_reads", "l1_hits", "l1_misses", "l1_bypasses",
        "l1_degraded", "l1_invalid_payloads", "l1_backfills",
        "l1_writes", "l1_write_degraded", "l2_reads", "l2_hits",
        "l2_misses", "l2_errors", "l2_writes", "l2_write_errors",
        "provider_calls", "provider_calls_avoided_by_l1",
        "provider_calls_avoided_by_l2",
    )

    def __init__(self, domain: str) -> None:
        self.domain = domain
        self._lock = threading.Lock()
        self._values = {field: 0 for field in self._FIELDS}

    def increment(self, field: str) -> None:
        with self._lock:
            self._values[field] += 1

    def snapshot(self) -> LayeredCacheMetricsSnapshot:
        with self._lock:
            values = dict(self._values)
        l1_total = values["l1_hits"] + values["l1_misses"]
        l2_total = values["l2_hits"] + values["l2_misses"]
        return LayeredCacheMetricsSnapshot(
            domain=self.domain,
            **values,
            l1_hit_rate=(values["l1_hits"] / l1_total if l1_total else 0.0),
            l2_hit_rate=(values["l2_hits"] / l2_total if l2_total else 0.0),
        )


class _LayeredAmapCache(Generic[TValue]):
    """路线与餐饮共享的两级缓存算法。"""

    def __init__(
        self,
        *,
        domain: str,
        model_type: type[TValue],
        l1_cache: CacheStore,
        l2_cache: Any,
        l1_key_builder: Callable[[str], str],
    ) -> None:
        self.domain = domain
        self.model_type = model_type
        self.l1_cache = l1_cache
        self.l2_cache = l2_cache
        self.l1_key_builder = l1_key_builder
        self._metrics = _LayeredCacheMetrics(domain)

    def lookup(self, cache_key: str) -> LayeredCacheLookup[TValue]:
        """按 L1、L2 顺序读取；L2 命中后按剩余 TTL 回填 L1。"""

        l1_key = self.l1_key_builder(cache_key)
        self._metrics.increment("l1_reads")
        try:
            l1_result = self.l1_cache.get(l1_key)
        except Exception:
            # 自定义 CacheStore 即使未遵守自动降级契约，也不能阻断 L2/Provider。
            self._metrics.increment("l1_degraded")
            logger.warning("%s L1 cache read failed", self.domain, exc_info=True)
            l1_result = None
            l1_status = "degraded"

        if l1_result is not None:
            l1_status = l1_result.status.value

        if l1_result is not None and l1_result.status == CacheReadStatus.HIT:
            try:
                value = self.model_type.model_validate(l1_result.value)
            except (ValidationError, TypeError, ValueError):
                # 通用 JSON 信封合法但领域结构已过期时，删除热缓存并继续查 L2。
                self._metrics.increment("l1_invalid_payloads")
                self._metrics.increment("l1_misses")
                l1_status = "invalid"
                try:
                    self.l1_cache.delete(l1_key)
                except Exception:
                    logger.warning("Failed to delete invalid %s L1 cache", self.domain, exc_info=True)
            else:
                self._metrics.increment("l1_hits")
                self._metrics.increment("provider_calls_avoided_by_l1")
                return LayeredCacheLookup(
                    source=LayeredCacheSource.L1,
                    value=value,
                    l1_status=l1_status,
                )
        elif l1_result is not None and l1_result.status == CacheReadStatus.MISS:
            self._metrics.increment("l1_misses")
        elif l1_result is not None and l1_result.status == CacheReadStatus.BYPASS:
            self._metrics.increment("l1_bypasses")
        elif l1_result is not None:
            self._metrics.increment("l1_degraded")

        self._metrics.increment("l2_reads")
        try:
            entry = self._get_l2_entry(cache_key)
        except Exception:
            self._metrics.increment("l2_errors")
            logger.warning("%s L2 cache read failed", self.domain, exc_info=True)
            return LayeredCacheLookup(
                source=LayeredCacheSource.MISS,
                l1_status=l1_status,
                l2_status="error",
            )

        if entry is None:
            self._metrics.increment("l2_misses")
            return LayeredCacheLookup(
                source=LayeredCacheSource.MISS,
                l1_status=l1_status,
                l2_status="miss",
            )

        self._metrics.increment("l2_hits")
        self._metrics.increment("provider_calls_avoided_by_l2")
        self._backfill_l1(l1_key, entry)
        return LayeredCacheLookup(
            source=LayeredCacheSource.L2,
            value=entry.value,
            l1_status=l1_status,
            l2_status="hit",
        )

    def get(self, cache_key: str) -> TValue | None:
        """兼容原 RouteCacheStore/RestaurantCacheStore 的简单读取接口。"""

        return self.lookup(cache_key).value

    def set(self, cache_key: str, value: TValue, *, ttl_seconds: int) -> None:
        """Provider 成功后独立写入 L2 和 L1；一层失败不影响另一层。"""

        try:
            self.l2_cache.set(cache_key, value, ttl_seconds=ttl_seconds)
            self._metrics.increment("l2_writes")
        except Exception:
            self._metrics.increment("l2_write_errors")
            logger.warning("%s L2 cache write failed", self.domain, exc_info=True)

        try:
            result = self.l1_cache.set(
                self.l1_key_builder(cache_key),
                value.model_dump(mode="json"),
                ttl_seconds=ttl_seconds,
            )
            if result.status == CacheWriteStatus.STORED:
                self._metrics.increment("l1_writes")
            elif result.status == CacheWriteStatus.DEGRADED:
                self._metrics.increment("l1_write_degraded")
        except Exception:
            self._metrics.increment("l1_write_degraded")
            logger.warning("%s L1 cache write failed", self.domain, exc_info=True)

    def purge_expired(self) -> int:
        """Redis 由 TTL 自动淘汰；显式清理只作用于 L2。"""

        return self.l2_cache.purge_expired()

    def record_provider_call(self) -> None:
        self._metrics.increment("provider_calls")

    def metrics_snapshot(self) -> LayeredCacheMetricsSnapshot:
        return self._metrics.snapshot()

    def _get_l2_entry(self, cache_key: str) -> CacheStoreEntry[TValue] | None:
        get_entry = getattr(self.l2_cache, "get_entry", None)
        if callable(get_entry):
            return get_entry(cache_key)
        # 兼容旧测试替身；无法获知剩余 TTL 时不回填 L1。
        value = self.l2_cache.get(cache_key)
        if value is None:
            return None
        return CacheStoreEntry(value=value, remaining_ttl_seconds=0)

    def _backfill_l1(
        self,
        l1_key: str,
        entry: CacheStoreEntry[TValue],
    ) -> None:
        if entry.remaining_ttl_seconds <= 0:
            return
        try:
            result = self.l1_cache.set(
                l1_key,
                entry.value.model_dump(mode="json"),
                ttl_seconds=entry.remaining_ttl_seconds,
            )
            if result.status == CacheWriteStatus.STORED:
                self._metrics.increment("l1_backfills")
                self._metrics.increment("l1_writes")
            elif result.status == CacheWriteStatus.DEGRADED:
                self._metrics.increment("l1_write_degraded")
        except Exception:
            self._metrics.increment("l1_write_degraded")
            logger.warning("Failed to backfill %s L1 cache", self.domain, exc_info=True)


class LayeredRouteCache(_LayeredAmapCache[RouteEstimate]):
    """高德路线 Redis L1 + 数据库 L2 缓存。"""

    def __init__(
        self,
        *,
        l1_cache: CacheStore,
        l2_cache: RouteCacheStore,
        l1_key_builder: Callable[[str], str],
    ) -> None:
        super().__init__(
            domain="route",
            model_type=RouteEstimate,
            l1_cache=l1_cache,
            l2_cache=l2_cache,
            l1_key_builder=l1_key_builder,
        )


class LayeredRestaurantCache(_LayeredAmapCache[RestaurantSearchSnapshot]):
    """高德餐饮候选 Redis L1 + 数据库 L2 缓存。"""

    def __init__(
        self,
        *,
        l1_cache: CacheStore,
        l2_cache: RestaurantCacheStore,
        l1_key_builder: Callable[[str], str],
    ) -> None:
        super().__init__(
            domain="restaurant",
            model_type=RestaurantSearchSnapshot,
            l1_cache=l1_cache,
            l2_cache=l2_cache,
            l1_key_builder=l1_key_builder,
        )
