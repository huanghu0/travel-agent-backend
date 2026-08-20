"""通用缓存抽象：接口、版本化 JSON、TTL、指标和 Store 工厂。"""

from app.infrastructure.cache.config import CacheConfig
from app.infrastructure.cache.factory import create_cache_store
from app.infrastructure.cache.interfaces import CacheStore
from app.infrastructure.cache.metrics import CacheMetrics, CacheMetricsSnapshot
from app.infrastructure.cache.models import (
    CacheLookup,
    CacheReadStatus,
    CacheWriteResult,
    CacheWriteStatus,
)
from app.infrastructure.cache.noop_store import NoOpCacheStore
from app.infrastructure.cache.serialization import (
    CacheEnvelope,
    CacheEnvelopeSerializer,
    CacheSchemaVersionError,
    CacheSerializationError,
)
from app.infrastructure.cache.ttl import CacheTTLDecision, CacheTTLPolicy
from app.infrastructure.cache.validation import MAX_CACHE_KEY_BYTES, validate_cache_key

__all__ = [
    "CacheConfig",
    "CacheEnvelope",
    "CacheEnvelopeSerializer",
    "CacheLookup",
    "CacheMetrics",
    "CacheMetricsSnapshot",
    "CacheReadStatus",
    "CacheSchemaVersionError",
    "CacheSerializationError",
    "CacheStore",
    "CacheTTLDecision",
    "CacheTTLPolicy",
    "CacheWriteResult",
    "CacheWriteStatus",
    "MAX_CACHE_KEY_BYTES",
    "NoOpCacheStore",
    "create_cache_store",
    "validate_cache_key",
]
