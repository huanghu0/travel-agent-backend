"""异步旅行规划任务的数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.trip_schema import TripRequest


def utc_now() -> datetime:
    """统一生成带 UTC 时区的时间，便于多进程租约比较。"""

    return datetime.now(timezone.utc)


TaskStatus = Literal[
    "queued",
    "running",
    "retrying",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
]

TerminalTaskStatus = Literal["succeeded", "failed", "cancelled", "timed_out"]

TaskEventType = Literal[
    "task_queued",
    "task_started",
    "task_recovered",
    "action_started",
    "action_completed",
    "action_retrying",
    "cancellation_requested",
    "task_succeeded",
    "task_failed",
    "task_cancelled",
    "task_timed_out",
]


class TaskFailureReport(BaseModel):
    """前端可直接展示和排障的结构化失败报告。"""

    code: str
    message: str
    stage: str = "执行循环"
    stage_name: str = "执行循环"
    action: str | None = None
    retryable: bool = False
    provider_code: str | None = None
    provider_message: str | None = None
    session_id: str
    current_step: int = Field(default=0, ge=0)
    max_steps: int = Field(default=0, ge=0)
    exception_type: str
    occurred_at: datetime = Field(default_factory=utc_now)
    details: dict[str, Any] = Field(default_factory=dict)


class TripPlanningTask(BaseModel):
    """持久化任务快照；页面刷新和服务重启都从 SQLite 重新读取。"""

    task_id: str
    session_id: str
    user_id: str | None = None
    idempotency_key: str
    request_fingerprint: str
    request: TripRequest
    status: TaskStatus = "queued"
    current_stage: str = "queued"
    stage_name: str = "等待执行"
    current_action: str | None = None
    progress_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    current_step: int = Field(default=0, ge=0)
    max_steps: int = Field(default=0, ge=0)
    attempt: int = Field(default=0, ge=0)
    recovery_count: int = Field(default=0, ge=0)
    message: str = "任务已进入队列"
    cancel_requested: bool = False
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result_session_id: str | None = None
    failure_report: TaskFailureReport | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled", "timed_out"}


class TripTaskEvent(BaseModel):
    """可回放的任务事件；event_id 同时作为 SSE 的 id。"""

    event_id: int = Field(ge=1)
    task_id: str
    event_type: TaskEventType
    stage: str
    stage_name: str
    progress_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    current_step: int = Field(default=0, ge=0)
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class TripTaskCreateResponse(BaseModel):
    """创建接口的轻量 202 响应。"""

    task_id: str
    session_id: str
    status: TaskStatus
    created_at: datetime
    reused: bool = False


class TripTaskCancelResponse(BaseModel):
    task_id: str
    status: TaskStatus
    cancel_requested: bool
    message: str
