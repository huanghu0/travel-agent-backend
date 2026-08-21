"""Redis 基础设施入口：配置、连接管理、健康检查和 Key 规范。"""

from app.infrastructure.redis.client import (
    RedisClientManager,
    RedisClientMetrics,
    RedisPoolSnapshot,
    RedisHealth,
    RedisHealthStatus,
    create_redis_client,
)
from app.infrastructure.redis.cache_store import RedisCacheStore
from app.infrastructure.redis.config import RedisConfig
from app.infrastructure.redis.keys import RedisKeyBuilder
from app.infrastructure.redis.task_notifications import (
    RedisTaskNotificationBus,
    create_task_notification_bus,
)

__all__ = [
    "RedisClientManager",
    "RedisClientMetrics",
    "RedisPoolSnapshot",
    "RedisConfig",
    "RedisHealth",
    "RedisHealthStatus",
    "RedisKeyBuilder",
    "RedisCacheStore",
    "RedisTaskNotificationBus",
    "create_redis_client",
    "create_task_notification_bus",
]
