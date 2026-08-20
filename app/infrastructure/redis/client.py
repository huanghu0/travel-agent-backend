"""Redis 连接池、健康检查和故障自动降级运行时。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, TypeVar
from urllib.parse import quote, quote_plus

from redis import Connection, ConnectionPool, Redis, SSLConnection
from redis.exceptions import RedisError

from app.infrastructure.redis.config import RedisConfig


logger = logging.getLogger(__name__)
T = TypeVar("T")


class RedisHealthStatus(str, Enum):
    DISABLED = "disabled"
    OK = "ok"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class RedisHealth:
    """Redis 组件健康状态；错误信息经过密码擦除。"""

    enabled: bool
    status: RedisHealthStatus
    target: str
    healthy: bool | None
    degraded: bool
    latency_ms: float | None = None
    error: str | None = None

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


def _safe_error_message(exc: Exception, config: RedisConfig) -> str:
    """压平异常并擦除原始及 URL 编码密码。"""

    message = " ".join(str(exc).split()) or exc.__class__.__name__
    if config.password:
        variants = {
            config.password,
            quote(config.password, safe=""),
            quote_plus(config.password, safe=""),
        }
        for secret in sorted(variants, key=len, reverse=True):
            if secret:
                message = message.replace(secret, "***")
    return message[:500]


def create_redis_client(config: RedisConfig) -> Redis:
    """创建线程安全连接池；实际 TCP 连接在首次命令时按需建立。"""

    pool = ConnectionPool(
        connection_class=SSLConnection if config.ssl else Connection,
        host=config.host,
        port=config.port,
        db=config.database,
        username=config.username,
        password=config.password,
        max_connections=config.max_connections,
        socket_connect_timeout=config.socket_connect_timeout_seconds,
        socket_timeout=config.socket_timeout_seconds,
        health_check_interval=config.health_check_interval_seconds,
        retry_on_timeout=config.retry_on_timeout,
        decode_responses=config.decode_responses,
        client_name=config.client_name,
    )
    return Redis(connection_pool=pool)


class RedisClientManager:
    """集中管理 Redis 客户端，并在故障时返回回退值而不是中断业务。"""

    def __init__(
        self,
        config: RedisConfig,
        *,
        client_factory: Callable[[RedisConfig], Redis] = create_redis_client,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._client: Redis | None = None
        self._degraded_until = 0.0
        self._last_error: str | None = None

    @property
    def key_builder(self):
        """延迟导入，避免连接管理模块与 Key 模块形成不必要耦合。"""

        from app.infrastructure.redis.keys import RedisKeyBuilder

        return RedisKeyBuilder(self.config.key_prefix)

    def get_client(self, *, force_retry: bool = False) -> Redis | None:
        """返回可尝试使用的客户端；禁用或冷却期内返回 None。"""

        if not self.config.enabled:
            return None
        with self._lock:
            if not force_retry and self._monotonic() < self._degraded_until:
                return None
            if self._client is None:
                self._client = self._client_factory(self.config)
            return self._client

    def execute(
        self,
        operation: Callable[[Redis], T],
        *,
        fallback: T | None = None,
    ) -> T | None:
        """执行非关键 Redis 操作；连接故障时自动降级并返回 fallback。"""

        try:
            client = self.get_client()
            if client is None:
                return fallback
            result = operation(client)
        except (RedisError, OSError, TimeoutError) as exc:
            # 客户端创建、连接获取和具体命令任一环节失败都进入同一降级路径。
            self._mark_degraded(exc)
            return fallback
        self._mark_healthy()
        return result

    def check_health(self) -> RedisHealth:
        """执行 PING；健康检查会绕过冷却期，以便 Redis 恢复后立即自愈。"""

        if not self.config.enabled:
            return RedisHealth(
                enabled=False,
                status=RedisHealthStatus.DISABLED,
                target=self.config.safe_target(),
                healthy=None,
                degraded=False,
            )

        started_at = self._monotonic()
        try:
            client = self.get_client(force_retry=True)
            if client is None or client.ping() is not True:
                raise ConnectionError("Redis PING 未返回成功")
        except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
            self._mark_degraded(exc)
            return RedisHealth(
                enabled=True,
                status=RedisHealthStatus.DEGRADED,
                target=self.config.safe_target(),
                healthy=False,
                degraded=True,
                latency_ms=round((self._monotonic() - started_at) * 1000, 3),
                error=_safe_error_message(exc, self.config),
            )

        self._mark_healthy()
        return RedisHealth(
            enabled=True,
            status=RedisHealthStatus.OK,
            target=self.config.safe_target(),
            healthy=True,
            degraded=False,
            latency_ms=round((self._monotonic() - started_at) * 1000, 3),
        )

    def _mark_healthy(self) -> None:
        with self._lock:
            self._degraded_until = 0.0
            self._last_error = None

    def _mark_degraded(self, exc: Exception) -> None:
        safe_error = _safe_error_message(exc, self.config)
        with self._lock:
            first_failure = self._last_error is None
            self._last_error = safe_error
            self._degraded_until = (
                self._monotonic() + self.config.degrade_cooldown_seconds
            )
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
                client.connection_pool.disconnect()
            except Exception:
                logger.debug("关闭 Redis 故障连接时发生非关键异常", exc_info=True)
        if first_failure:
            logger.warning(
                "Redis 不可用，已自动降级到持久化后端或直连 Provider: target=%s error=%s",
                self.config.safe_target(),
                safe_error,
            )

    def close(self) -> None:
        """应用关闭时释放连接池；重复调用安全。"""

        with self._lock:
            client = self._client
            self._client = None
        if client is None:
            return
        try:
            client.close()
            client.connection_pool.disconnect()
        except Exception:
            # Redis 是非关键组件，连接池清理错误只记录，不阻塞 FastAPI 退出。
            logger.warning("关闭 Redis 连接池失败", exc_info=True)
