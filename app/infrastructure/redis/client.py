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
class RedisClientMetrics:
    """Redis 客户端累计指标；只记录连接运行状态，不记录 Key 或业务参数。"""

    operation_requests: int = 0
    operation_successes: int = 0
    operation_failures: int = 0
    operation_bypasses: int = 0
    health_checks: int = 0
    health_check_failures: int = 0
    degraded_transitions: int = 0
    recoveries: int = 0

    def model_dump(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RedisPoolSnapshot:
    """连接池容量快照；读取 redis-py 池状态时保持容错。"""

    max_connections: int
    created_connections: int
    in_use_connections: int
    available_connections: int
    utilization: float

    def model_dump(self) -> dict[str, int | float]:
        return asdict(self)


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
        self._degraded = False
        self._degraded_since = 0.0
        self._metrics: dict[str, int] = {
            name: 0 for name in RedisClientMetrics.__dataclass_fields__
        }

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

        with self._lock:
            self._metrics["operation_requests"] += 1
        try:
            client = self.get_client()
            if client is None:
                with self._lock:
                    self._metrics["operation_bypasses"] += 1
                return fallback
            result = operation(client)
        except (RedisError, OSError, TimeoutError) as exc:
            with self._lock:
                self._metrics["operation_failures"] += 1
            # 客户端创建、连接获取和具体命令任一环节失败都进入同一降级路径。
            self._mark_degraded(exc)
            return fallback
        with self._lock:
            self._metrics["operation_successes"] += 1
        self._mark_healthy()
        return result

    def metrics_snapshot(self) -> RedisClientMetrics:
        """返回线程安全累计指标，供 Prometheus/OpenTelemetry 读取。"""

        with self._lock:
            return RedisClientMetrics(**self._metrics)

    def pool_snapshot(self) -> RedisPoolSnapshot:
        """返回当前进程连接池使用量；Redis 未创建连接时数值为零。"""

        with self._lock:
            client = self._client
            max_connections = max(1, int(self.config.max_connections))
        if client is None:
            return RedisPoolSnapshot(max_connections, 0, 0, 0, 0.0)
        pool = getattr(client, "connection_pool", None)
        lock = getattr(pool, "_lock", None)
        if lock is None:
            return self._read_pool_snapshot(pool, max_connections)
        with lock:
            return self._read_pool_snapshot(pool, max_connections)

    @staticmethod
    def _read_pool_snapshot(pool: Any, max_connections: int) -> RedisPoolSnapshot:
        available = len(getattr(pool, "_available_connections", ()) or ())
        in_use = len(getattr(pool, "_in_use_connections", ()) or ())
        created = int(getattr(pool, "_created_connections", available + in_use) or 0)
        utilization = min(1.0, max(0.0, in_use / max_connections))
        return RedisPoolSnapshot(
            max_connections=max_connections,
            created_connections=created,
            in_use_connections=in_use,
            available_connections=available,
            utilization=utilization,
        )

    def degraded_duration_seconds(self) -> float:
        """返回当前连续降级时长；恢复后立即归零。"""

        with self._lock:
            if not self._degraded or self._degraded_since <= 0:
                return 0.0
            return max(0.0, self._monotonic() - self._degraded_since)

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

        with self._lock:
            self._metrics["health_checks"] += 1
        started_at = self._monotonic()
        try:
            client = self.get_client(force_retry=True)
            if client is None or client.ping() is not True:
                raise ConnectionError("Redis PING 未返回成功")
        except (RedisError, OSError, TimeoutError, ConnectionError) as exc:
            with self._lock:
                self._metrics["health_check_failures"] += 1
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
            if self._degraded:
                self._metrics["recoveries"] += 1
            self._degraded = False
            self._degraded_since = 0.0
            self._degraded_until = 0.0
            self._last_error = None

    def report_healthy(self) -> None:
        """供 Pub/Sub 等长连接在成功建立后同步健康状态。"""

        self._mark_healthy()

    def _mark_degraded(self, exc: Exception) -> None:
        safe_error = _safe_error_message(exc, self.config)
        with self._lock:
            first_failure = not self._degraded
            if first_failure:
                self._degraded = True
                self._degraded_since = self._monotonic()
                self._metrics["degraded_transitions"] += 1
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

    def report_failure(self, exc: Exception) -> None:
        """供不经过 ``execute`` 的长连接统一进入自动降级冷却。"""

        self._mark_degraded(exc)

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
