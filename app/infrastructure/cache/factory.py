"""按配置创建 Redis 或 NoOp 通用缓存 Store。"""

from __future__ import annotations

from app.infrastructure.cache.config import CacheConfig
from app.infrastructure.cache.interfaces import CacheStore
from app.infrastructure.cache.noop_store import NoOpCacheStore
from app.infrastructure.cache.serialization import CacheEnvelopeSerializer
from app.infrastructure.cache.ttl import CacheTTLPolicy


def create_cache_store(*, cache_config: CacheConfig, redis_client_manager) -> CacheStore:
    """Redis 未启用时返回 NoOp，业务调用方不需要分支判断。"""

    if not cache_config.enabled:
        return NoOpCacheStore(schema_version=cache_config.schema_version)

    # 延迟导入 Redis 实现，保持通用缓存协议不依赖具体客户端。
    from app.infrastructure.redis.cache_store import RedisCacheStore

    return RedisCacheStore(
        redis_client_manager,
        serializer=CacheEnvelopeSerializer(cache_config.schema_version),
        ttl_policy=CacheTTLPolicy(
            default_seconds=cache_config.default_ttl_seconds,
            min_seconds=cache_config.min_ttl_seconds,
            max_seconds=cache_config.max_ttl_seconds,
        ),
        delete_invalid_entries=cache_config.delete_invalid_entries,
    )
