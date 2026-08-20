"""版本化缓存 JSON 信封和严格反序列化。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable


class CacheSerializationError(ValueError):
    """缓存值不符合项目 JSON 序列化规范。"""


class CacheSchemaVersionError(CacheSerializationError):
    """缓存信封版本与当前代码不兼容。"""


@dataclass(frozen=True, slots=True)
class CacheEnvelope:
    schema_version: int
    created_at: datetime
    expires_at: datetime
    payload: Any

class CacheEnvelopeSerializer:
    """把缓存值编码成 UTF-8 JSON，并携带版本和绝对过期时间。"""

    def __init__(
        self,
        schema_version: int,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise TypeError("缓存 schema_version 必须是整数")
        if schema_version <= 0:
            raise ValueError("缓存 schema_version 必须大于 0")
        self.schema_version = schema_version
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _json_default(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Enum):
            return value.value
        raise TypeError(f"不支持的缓存值类型: {type(value).__name__}")

    def dumps(self, payload: Any, *, ttl_seconds: int) -> bytes:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise CacheSerializationError("缓存 TTL 必须是整数秒")
        if ttl_seconds <= 0:
            raise CacheSerializationError("缓存 TTL 必须大于 0")
        created_at = self._aware_utc(self._clock(), "created_at")
        expires_at = created_at.timestamp() + ttl_seconds
        envelope = {
            "schema_version": self.schema_version,
            "created_at": created_at.isoformat(),
            "expires_at": datetime.fromtimestamp(
                expires_at, tz=timezone.utc
            ).isoformat(),
            "payload": payload,
        }
        try:
            return json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=self._json_default,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CacheSerializationError(str(exc)) from exc

    def loads(self, raw: bytes | str) -> CacheEnvelope:
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise CacheSerializationError("缓存内容不是合法 UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise CacheSerializationError("缓存信封必须是 JSON 对象")
        required = {"schema_version", "created_at", "expires_at", "payload"}
        if set(data) != required:
            raise CacheSerializationError("缓存信封字段必须与当前 JSON 规范完全一致")
        version = data["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise CacheSerializationError("缓存 schema_version 必须是整数")
        if version != self.schema_version:
            raise CacheSchemaVersionError(
                f"缓存 schema_version={version!r}，当前版本={self.schema_version}"
            )
        try:
            created_at = self._aware_utc(
                datetime.fromisoformat(data["created_at"]), "created_at"
            )
            expires_at = self._aware_utc(
                datetime.fromisoformat(data["expires_at"]), "expires_at"
            )
        except (TypeError, ValueError) as exc:
            raise CacheSerializationError("缓存信封时间字段无效") from exc
        if expires_at <= created_at:
            raise CacheSerializationError("缓存 expires_at 必须晚于 created_at")
        return CacheEnvelope(
            schema_version=version,
            created_at=created_at,
            expires_at=expires_at,
            payload=data["payload"],
        )

    def is_expired(self, envelope: CacheEnvelope) -> bool:
        """使用同一时钟判断绝对过期时间，便于确定性测试。"""

        now = self._aware_utc(self._clock(), "now")
        return envelope.expires_at <= now

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise CacheSerializationError(f"缓存 {field_name} 必须包含时区")
        return value.astimezone(timezone.utc)
