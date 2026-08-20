"""统一 TTL 规则：默认值、非正数跳过和最大值封顶。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CacheTTLDecision:
    cacheable: bool
    seconds: int | None
    adjusted: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CacheTTLPolicy:
    default_seconds: int = 1800
    min_seconds: int = 1
    max_seconds: int = 604800

    def __post_init__(self) -> None:
        values = (self.default_seconds, self.min_seconds, self.max_seconds)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise TypeError("缓存 TTL 配置必须是整数秒")
        if self.min_seconds <= 0:
            raise ValueError("缓存最小 TTL 必须大于 0")
        if self.max_seconds < self.min_seconds:
            raise ValueError("缓存最大 TTL 不能小于最小 TTL")
        if not self.min_seconds <= self.default_seconds <= self.max_seconds:
            raise ValueError("缓存默认 TTL 必须位于最小值和最大值之间")

    def resolve(self, ttl_seconds: int | None) -> CacheTTLDecision:
        requested = self.default_seconds if ttl_seconds is None else ttl_seconds
        if isinstance(requested, bool) or not isinstance(requested, int):
            raise TypeError("缓存 TTL 必须是整数秒")
        if requested <= 0:
            return CacheTTLDecision(
                cacheable=False,
                seconds=None,
                adjusted=False,
                reason="non_positive_ttl",
            )
        if requested < self.min_seconds:
            return CacheTTLDecision(
                cacheable=True,
                seconds=self.min_seconds,
                adjusted=True,
                reason="clamped_to_minimum",
            )
        if requested > self.max_seconds:
            return CacheTTLDecision(
                cacheable=True,
                seconds=self.max_seconds,
                adjusted=True,
                reason="clamped_to_maximum",
            )
        return CacheTTLDecision(
            cacheable=True,
            seconds=requested,
            adjusted=False,
        )
