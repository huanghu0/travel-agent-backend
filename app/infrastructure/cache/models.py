"""通用缓存读写结果模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class CacheReadStatus(str, Enum):
    HIT = "hit"
    MISS = "miss"
    BYPASS = "bypass"
    DEGRADED = "degraded"


class CacheWriteStatus(str, Enum):
    STORED = "stored"
    SKIPPED = "skipped"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """显式区分命中、未命中、禁用绕过和 Redis 故障降级。"""

    status: CacheReadStatus
    value: Any = None
    reason: str | None = None

    @property
    def hit(self) -> bool:
        return self.status == CacheReadStatus.HIT


@dataclass(frozen=True, slots=True)
class CacheWriteResult:
    """缓存写入结果；业务层始终可继续写入持久化后端。"""

    status: CacheWriteStatus
    ttl_seconds: int | None = None
    reason: str | None = None

    @property
    def stored(self) -> bool:
        return self.status == CacheWriteStatus.STORED
