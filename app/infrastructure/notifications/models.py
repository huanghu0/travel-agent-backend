"""任务通知消息模型；消息可丢失，业务状态必须回到数据库读取。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal


NotificationKind = Literal["task_available", "task_event", "cancellation"]
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True, slots=True)
class TaskNotificationMessage:
    """Redis Pub/Sub 使用的最小版本化消息，不承载行程或用户输入。"""

    kind: NotificationKind
    task_id: str
    event_id: int | None = None
    event_type: str | None = None
    schema_version: int = 1
    published_at: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("不支持的任务通知 schema_version")
        if not _SAFE_TASK_ID.fullmatch(self.task_id):
            raise ValueError("任务通知 task_id 格式无效")
        if self.event_id is not None and self.event_id < 1:
            raise ValueError("任务通知 event_id 必须大于 0")
        if self.kind not in {"task_available", "task_event", "cancellation"}:
            raise ValueError("任务通知 kind 无效")

    def to_json(self) -> str:
        payload = asdict(self)
        if not payload["published_at"]:
            payload["published_at"] = datetime.now(timezone.utc).isoformat()
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> "TaskNotificationMessage":
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode("utf-8")
        raw: Any = json.loads(payload)
        if not isinstance(raw, dict):
            raise ValueError("任务通知必须是 JSON 对象")
        return cls(
            schema_version=int(raw.get("schema_version", 0)),
            kind=raw.get("kind"),
            task_id=str(raw.get("task_id", "")),
            event_id=(int(raw["event_id"]) if raw.get("event_id") is not None else None),
            event_type=(str(raw["event_type"]) if raw.get("event_type") else None),
            published_at=str(raw.get("published_at", "")),
        )
