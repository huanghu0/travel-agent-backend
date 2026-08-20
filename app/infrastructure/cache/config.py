"""通用缓存策略配置。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CacheConfig:
    """缓存信封版本、TTL 边界和损坏数据处理策略。"""

    enabled: bool = False
    schema_version: int = 1
    default_ttl_seconds: int = 1800
    min_ttl_seconds: int = 1
    max_ttl_seconds: int = 604800
    delete_invalid_entries: bool = True

    def __post_init__(self) -> None:
        integer_fields = {
            "schema_version": self.schema_version,
            "default_ttl_seconds": self.default_ttl_seconds,
            "min_ttl_seconds": self.min_ttl_seconds,
            "max_ttl_seconds": self.max_ttl_seconds,
        }
        invalid_integer = any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in integer_fields.values()
        )
        if invalid_integer:
            raise TypeError("缓存版本和 TTL 配置必须是整数")
        if self.schema_version <= 0:
            raise ValueError("缓存 schema_version 必须大于 0")
        if self.min_ttl_seconds <= 0:
            raise ValueError("缓存最小 TTL 必须大于 0")
        if self.max_ttl_seconds < self.min_ttl_seconds:
            raise ValueError("缓存最大 TTL 不能小于最小 TTL")
        if not self.min_ttl_seconds <= self.default_ttl_seconds <= self.max_ttl_seconds:
            raise ValueError("缓存默认 TTL 必须位于最小值和最大值之间")

    @classmethod
    def from_settings(cls, settings: Any) -> "CacheConfig":
        return cls(
            enabled=settings.REDIS_ENABLED,
            schema_version=settings.REDIS_CACHE_SCHEMA_VERSION,
            default_ttl_seconds=settings.REDIS_DEFAULT_TTL_SECONDS,
            min_ttl_seconds=settings.REDIS_CACHE_MIN_TTL_SECONDS,
            max_ttl_seconds=settings.REDIS_CACHE_MAX_TTL_SECONDS,
            delete_invalid_entries=settings.REDIS_CACHE_DELETE_INVALID_ENTRIES,
        )
