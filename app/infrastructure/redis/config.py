"""Redis 连接配置；只保存连接参数，不在日志中暴露密码。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RedisConfig:
    """创建 Redis 连接池和自动降级运行时所需的配置。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6379
    database: int = 0
    username: str | None = None
    password: str | None = None
    ssl: bool = False
    max_connections: int = 20
    socket_connect_timeout_seconds: float = 3.0
    socket_timeout_seconds: float = 5.0
    health_check_interval_seconds: int = 30
    retry_on_timeout: bool = True
    decode_responses: bool = False
    client_name: str = "travel-agent-backend"
    key_prefix: str = "travel-agent:dev"
    default_ttl_seconds: int = 1800
    degrade_cooldown_seconds: float = 5.0

    @classmethod
    def from_settings(cls, settings: Any) -> "RedisConfig":
        """从项目 Settings 创建配置，便于测试注入轻量替身。"""

        return cls(
            enabled=settings.REDIS_ENABLED,
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            database=settings.REDIS_DB,
            username=settings.REDIS_USERNAME,
            password=settings.REDIS_PASSWORD,
            ssl=settings.REDIS_SSL,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            socket_connect_timeout_seconds=(
                settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS
            ),
            socket_timeout_seconds=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            health_check_interval_seconds=(
                settings.REDIS_HEALTH_CHECK_INTERVAL_SECONDS
            ),
            retry_on_timeout=settings.REDIS_RETRY_ON_TIMEOUT,
            decode_responses=settings.REDIS_DECODE_RESPONSES,
            client_name=settings.REDIS_CLIENT_NAME,
            key_prefix=settings.REDIS_KEY_PREFIX,
            default_ttl_seconds=settings.REDIS_DEFAULT_TTL_SECONDS,
            degrade_cooldown_seconds=settings.REDIS_DEGRADE_COOLDOWN_SECONDS,
        )

    def safe_target(self) -> str:
        """返回不包含用户名和密码的诊断目标。"""

        scheme = "rediss" if self.ssl else "redis"
        return f"{scheme}://{self.host}:{self.port}/{self.database}"
