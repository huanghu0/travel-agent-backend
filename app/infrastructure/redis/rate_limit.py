"""Redis 跨实例固定窗口限流与供应商配额控制。

Redis 只负责协调多个 API/Worker 实例的额度计数；Redis 不可用时默认 fail-open，
避免缓存/协调层故障阻断旅行规划主流程，同时通过指标暴露降级次数。
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.infrastructure.redis.client import RedisClientManager
from app.infrastructure.redis.keys import RedisKeyBuilder


# 单个 Lua 脚本原子完成计数和过期时间设置，多个进程不会各自超发额度。
_FIXED_WINDOW_LUA = """
local current = redis.call('INCRBY', KEYS[1], ARGV[1])
if current == tonumber(ARGV[1]) then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
local ttl = redis.call('PTTL', KEYS[1])
return {current, ttl}
"""
_DEGRADED = object()


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """一个低基数固定窗口策略；limit<=0 表示关闭该策略。"""

    name: str
    limit: int
    window_seconds: int

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self.window_seconds > 0


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float
    limit: int
    window_seconds: int
    degraded: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RateLimitMetricsSnapshot:
    checks: int
    allowed: int
    rejected: int
    degraded_allowed: int
    degraded_rejected: int
    redis_failures: int
    provider_allowed: dict[str, int]
    provider_rejected: dict[str, int]

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class RateLimiter(Protocol):
    def acquire(
        self,
        *,
        provider: str,
        policy: QuotaPolicy,
        identity: str = "global",
        cost: int = 1,
    ) -> RateLimitDecision: ...

    def metrics_snapshot(self) -> RateLimitMetricsSnapshot: ...


class _RateLimitMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = {
            "checks": 0,
            "allowed": 0,
            "rejected": 0,
            "degraded_allowed": 0,
            "degraded_rejected": 0,
            "redis_failures": 0,
        }
        self._provider_allowed: dict[str, int] = {}
        self._provider_rejected: dict[str, int] = {}

    def record(self, provider: str, decision: RateLimitDecision) -> None:
        with self._lock:
            self._values["checks"] += 1
            outcome = "allowed" if decision.allowed else "rejected"
            self._values[outcome] += 1
            target = self._provider_allowed if decision.allowed else self._provider_rejected
            target[provider] = target.get(provider, 0) + 1
            if decision.degraded:
                self._values["redis_failures"] += 1
                self._values[f"degraded_{outcome}"] += 1

    def snapshot(self) -> RateLimitMetricsSnapshot:
        with self._lock:
            return RateLimitMetricsSnapshot(
                **self._values,
                provider_allowed=dict(self._provider_allowed),
                provider_rejected=dict(self._provider_rejected),
            )


class NoOpRateLimiter:
    """Redis 或业务限流关闭时的空实现。"""

    def __init__(self) -> None:
        self._metrics = _RateLimitMetrics()

    def acquire(
        self,
        *,
        provider: str,
        policy: QuotaPolicy,
        identity: str = "global",
        cost: int = 1,
    ) -> RateLimitDecision:
        del identity, cost
        decision = RateLimitDecision(
            allowed=True,
            remaining=max(0, policy.limit),
            retry_after_seconds=0.0,
            limit=max(0, policy.limit),
            window_seconds=max(0, policy.window_seconds),
            reason="rate_limit_disabled",
        )
        self._metrics.record(provider, decision)
        return decision

    def metrics_snapshot(self) -> RateLimitMetricsSnapshot:
        return self._metrics.snapshot()


class RedisRateLimiter:
    """使用 Redis Lua 的跨实例固定窗口限流器。"""

    def __init__(
        self,
        client_manager: RedisClientManager,
        *,
        key_builder: RedisKeyBuilder,
        fail_open: bool = True,
    ) -> None:
        self.client_manager = client_manager
        self.key_builder = key_builder
        self.fail_open = fail_open
        self._metrics = _RateLimitMetrics()

    def acquire(
        self,
        *,
        provider: str,
        policy: QuotaPolicy,
        identity: str = "global",
        cost: int = 1,
    ) -> RateLimitDecision:
        if cost <= 0:
            raise ValueError("限流 cost 必须大于 0")
        if not policy.enabled:
            decision = RateLimitDecision(
                allowed=True,
                remaining=max(0, policy.limit),
                retry_after_seconds=0.0,
                limit=max(0, policy.limit),
                window_seconds=max(0, policy.window_seconds),
                reason="policy_disabled",
            )
            self._metrics.record(provider, decision)
            return decision

        key = self.key_builder.quota(
            provider=provider,
            policy=policy.name,
            identity=identity,
        )
        result = self.client_manager.execute(
            lambda client: client.eval(
                _FIXED_WINDOW_LUA,
                1,
                key,
                int(cost),
                int(policy.window_seconds * 1000),
            ),
            fallback=_DEGRADED,
        )
        if result is _DEGRADED or not isinstance(result, (list, tuple)) or len(result) < 2:
            decision = RateLimitDecision(
                allowed=self.fail_open,
                remaining=policy.limit if self.fail_open else 0,
                retry_after_seconds=0.0 if self.fail_open else float(policy.window_seconds),
                limit=policy.limit,
                window_seconds=policy.window_seconds,
                degraded=True,
                reason="redis_unavailable",
            )
            self._metrics.record(provider, decision)
            return decision

        current = int(result[0])
        ttl_ms = max(0, int(result[1]))
        allowed = current <= policy.limit
        decision = RateLimitDecision(
            allowed=allowed,
            remaining=max(0, policy.limit - current),
            retry_after_seconds=0.0 if allowed else round(ttl_ms / 1000.0, 3),
            limit=policy.limit,
            window_seconds=policy.window_seconds,
            reason=None if allowed else "quota_exceeded",
        )
        self._metrics.record(provider, decision)
        return decision

    def metrics_snapshot(self) -> RateLimitMetricsSnapshot:
        return self._metrics.snapshot()


class ProviderQuotaExceededError(RuntimeError):
    """本地跨实例供应商额度耗尽，不包含密钥或用户输入。"""

    def __init__(self, provider: str, policy: QuotaPolicy, decision: RateLimitDecision):
        super().__init__(
            f"{provider} quota exceeded: {policy.name}; "
            f"retry_after={decision.retry_after_seconds}s"
        )
        self.provider = provider
        self.policy = policy
        self.decision = decision


class ProviderQuotaController:
    """把高德/LLM 多个限流窗口组合成统一请求前检查。"""

    def __init__(
        self,
        limiter: RateLimiter,
        *,
        amap_policies: tuple[QuotaPolicy, ...] = (),
        llm_policies: tuple[QuotaPolicy, ...] = (),
    ) -> None:
        self.limiter = limiter
        self.amap_policies = tuple(item for item in amap_policies if item.enabled)
        self.llm_policies = tuple(item for item in llm_policies if item.enabled)

    def acquire_amap(self) -> None:
        self._acquire("amap", self.amap_policies, identity="global")

    def acquire_llm(self, model: str) -> None:
        # identity 会在 RedisKeyBuilder 中做 SHA-256，不把模型或网关信息直接写入 Key。
        self._acquire("llm", self.llm_policies, identity=model or "default")

    def _acquire(
        self,
        provider: str,
        policies: tuple[QuotaPolicy, ...],
        *,
        identity: str,
    ) -> None:
        for policy in policies:
            decision = self.limiter.acquire(
                provider=provider,
                policy=policy,
                identity=identity,
            )
            if not decision.allowed:
                raise ProviderQuotaExceededError(provider, policy, decision)

    def metrics_snapshot(self) -> RateLimitMetricsSnapshot:
        return self.limiter.metrics_snapshot()


_controller: ProviderQuotaController | None = None


def configure_provider_quota_controller(controller: ProviderQuotaController | None) -> None:
    """应用启动时注入共享控制器；测试默认不启用全局额度。"""

    global _controller
    _controller = controller


def get_provider_quota_controller() -> ProviderQuotaController | None:
    return _controller
