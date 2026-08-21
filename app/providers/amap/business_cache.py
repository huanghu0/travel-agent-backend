"""高德标准化结果的 Redis-only 业务缓存。

路线和餐饮已有 Redis L1 → MySQL L2；天气、景点、酒店和地理编码属于
可重建查询结果，本阶段先使用 Redis L1，未命中或降级时直接回源高德。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.infrastructure.cache import CacheReadStatus, CacheStore, CacheWriteStatus
from app.infrastructure.redis.keys import RedisKeyBuilder


logger = logging.getLogger(__name__)
TModel = TypeVar("TModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AmapBusinessCacheMetricsSnapshot:
    domains: dict[str, dict[str, int]]

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class AmapBusinessCache:
    """按领域记录命中率，并保证任何缓存异常都不阻断 Provider。"""

    def __init__(self, cache_store: CacheStore, key_builder: RedisKeyBuilder) -> None:
        self.cache_store = cache_store
        self.key_builder = key_builder
        self._lock = threading.Lock()
        self._domains: dict[str, dict[str, int]] = {}

    def get_or_load(
        self,
        *,
        domain: str,
        key_payload: Any,
        model_type: type[TModel],
        ttl_seconds: int,
        loader: Callable[[], TModel],
    ) -> TModel:
        key = self.key_builder.business_cache(domain, key_payload)
        try:
            lookup = self.cache_store.get(key)
        except Exception:
            self._increment(domain, "degraded_reads")
            logger.warning("%s Redis business cache read failed", domain, exc_info=True)
            lookup = None

        if lookup is not None and lookup.status == CacheReadStatus.HIT:
            try:
                value = model_type.model_validate(lookup.value)
            except (ValidationError, TypeError, ValueError):
                self._increment(domain, "invalid_payloads")
                try:
                    self.cache_store.delete(key)
                except Exception:
                    pass
            else:
                self._increment(domain, "hits")
                return value
        elif lookup is not None:
            if lookup.status == CacheReadStatus.DEGRADED:
                self._increment(domain, "degraded_reads")
            elif lookup.status == CacheReadStatus.BYPASS:
                self._increment(domain, "bypasses")
            else:
                self._increment(domain, "misses")

        self._increment(domain, "provider_calls")
        value = loader()
        try:
            result = self.cache_store.set(
                key,
                value.model_dump(mode="json"),
                ttl_seconds=ttl_seconds,
            )
            if result.status == CacheWriteStatus.STORED:
                self._increment(domain, "writes")
            elif result.status == CacheWriteStatus.DEGRADED:
                self._increment(domain, "degraded_writes")
        except Exception:
            self._increment(domain, "degraded_writes")
            logger.warning("%s Redis business cache write failed", domain, exc_info=True)
        return value

    def delete(self, domain: str, key_payload: Any) -> bool:
        try:
            return self.cache_store.delete(
                self.key_builder.business_cache(domain, key_payload)
            )
        except Exception:
            return False

    def _increment(self, domain: str, field: str) -> None:
        with self._lock:
            counters = self._domains.setdefault(
                domain,
                {
                    "hits": 0,
                    "misses": 0,
                    "bypasses": 0,
                    "degraded_reads": 0,
                    "invalid_payloads": 0,
                    "writes": 0,
                    "degraded_writes": 0,
                    "provider_calls": 0,
                },
            )
            counters[field] += 1

    def metrics_snapshot(self) -> AmapBusinessCacheMetricsSnapshot:
        with self._lock:
            return AmapBusinessCacheMetricsSnapshot(
                domains={name: dict(values) for name, values in self._domains.items()}
            )
