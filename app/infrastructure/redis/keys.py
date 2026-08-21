"""Redis Key 命名规范，避免业务代码散落字符串拼接和原始查询条件。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class RedisKeyBuilder:
    """构建带环境前缀、稳定摘要且长度受控的 Redis Key。"""

    def __init__(self, prefix: str, *, max_length: int = 512) -> None:
        self.prefix = self._normalize_prefix(prefix)
        if max_length < len(self.prefix) + 16:
            raise ValueError("Redis Key 最大长度过小")
        self.max_length = max_length

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        parts = [part.strip() for part in prefix.strip().strip(":").split(":")]
        if not parts or any(not part for part in parts):
            raise ValueError("REDIS_KEY_PREFIX 不能为空或包含空段")
        for part in parts:
            RedisKeyBuilder._validate_literal(part, "Key 前缀")
        return ":".join(parts)

    @staticmethod
    def _validate_literal(value: str, label: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{label}不能为空")
        if len(normalized) > 128 or not _SAFE_SEGMENT.fullmatch(normalized):
            raise ValueError(
                f"{label}只能包含字母、数字、点、下划线和短横线，且不超过 128 字符"
            )
        return normalized

    @staticmethod
    def _json_value(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if is_dataclass(value):
            return asdict(value)
        return value

    @classmethod
    def fingerprint(cls, payload: Any) -> str:
        """对查询参数生成稳定摘要，避免把地址或偏好原文写入 Redis Key。"""

        canonical = json.dumps(
            cls._json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def literal(self, namespace: str, *parts: str) -> str:
        """构建只包含可信标识符的 Key，例如 task_id 和 session_id。"""

        segments = [
            self.prefix,
            self._validate_literal(namespace, "Key 命名空间"),
            *(self._validate_literal(part, "Key 标识符") for part in parts),
        ]
        return self._ensure_length(":".join(segments))

    def hashed(self, namespace: str, payload: Any) -> str:
        """构建用户输入或复杂查询参数的摘要 Key。"""

        return self.literal(namespace, self.fingerprint(payload))

    def route(self, payload: Any) -> str:
        return self.literal("cache", "route", self.fingerprint(payload))

    def restaurant(self, payload: Any) -> str:
        return self.literal("cache", "restaurant", self.fingerprint(payload))

    def place(self, payload: Any) -> str:
        return self.literal("cache", "place", self.fingerprint(payload))

    def weather(self, payload: Any) -> str:
        return self.literal("cache", "weather", self.fingerprint(payload))

    def business_cache(self, domain: str, payload: Any) -> str:
        """构建天气、景点、酒店和地理编码等可重建结果的缓存 Key。"""

        return self.literal(
            "cache",
            self._validate_literal(domain, "缓存领域"),
            self.fingerprint(payload),
        )

    def execution_view(self, session_id: str) -> str:
        return self.literal("snapshot", "execution-view", session_id)

    def quota(self, *, provider: str, policy: str, identity: str) -> str:
        """额度 Key 不包含 API Key、模型原文或用户输入。"""

        return self.literal(
            "quota",
            self._validate_literal(provider, "供应商"),
            self._validate_literal(policy, "配额策略"),
            self.fingerprint(identity),
        )

    def task_progress(self, task_id: str) -> str:
        return self.literal("task", "progress", task_id)

    def task_notification_channel(self) -> str:
        """Worker 新任务通知频道；频道名只由可信环境前缀构成。"""

        return self.literal("notify", "tasks")

    def task_event_channel(self) -> str:
        """SSE 事件唤醒频道；真实事件仍从 MySQL/SQLite 回放。"""

        return self.literal("notify", "events")

    def task_cancellation_channel(self) -> str:
        """执行中取消的快速广播频道。"""

        return self.literal("notify", "cancellations")

    def session(self, session_id: str) -> str:
        return self.literal("session", session_id)

    def lock(self, namespace: str, payload: Any) -> str:
        lock_namespace = self._validate_literal(namespace, "锁命名空间")
        return self.literal("lock", lock_namespace, self.fingerprint(payload))

    def _ensure_length(self, key: str) -> str:
        if len(key.encode("utf-8")) > self.max_length:
            raise ValueError("Redis Key 超过最大长度")
        return key
