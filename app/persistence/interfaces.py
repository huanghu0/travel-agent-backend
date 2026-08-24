"""业务层使用的数据库后端无关 Store 接口。

所有接口都采用结构化 Protocol：Orchestrator、Worker 和服务层只依赖这些能力，
不再感知底层是 SQLite、MySQL，或测试中的内存替身。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TYPE_CHECKING, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from app.agent_runtime.state import AgentState, AgentStatus
    from app.evaluation.models import FixedAcceptanceBaselineReport
    from app.memory.models import (
        AgentSessionSummary,
        ExecutionBaselineReport,
        QualityBaselineReport,
    )
    from app.providers.amap.models import (
        RestaurantSearchSnapshot,
        RouteEstimate,
    )
    from app.schemas.trip_draft_schema import TripDraft, TripPlanVersion
    from app.schemas.trip_schema import TripRequest
    from app.task_runtime.models import (
        TaskEventType,
        TaskFailureReport,
        TripPlanningTask,
        TripTaskEvent,
    )


TCacheValue = TypeVar("TCacheValue")


@dataclass(frozen=True, slots=True)
class CacheStoreEntry(Generic[TCacheValue]):
    """L2 持久化缓存条目，并携带剩余 TTL 供 Redis 回填使用。"""

    value: TCacheValue
    remaining_ttl_seconds: int


@runtime_checkable
class AgentStateStore(Protocol):
    """AgentState 检查点、会话查询和质量基线存储接口。"""

    def create_state(self, state: AgentState) -> None: ...

    def save_state(self, state: AgentState) -> None: ...

    def get_state(self, session_id: str, *, user_id: str | None = None) -> AgentState: ...

    def delete_session(self, session_id: str, *, user_id: str) -> list[str]: ...

    def list_sessions(
        self,
        *,
        limit: int = 50,
        status: AgentStatus | None = None,
        user_id: str | None = None,
    ) -> list[AgentSessionSummary]: ...

    def get_execution_baseline(
        self,
        *,
        limit: int = 1000,
        status: AgentStatus | None = None,
        city: str | None = None,
        top_n: int = 20,
        max_cycle_span: int = 12,
        user_id: str | None = None,
    ) -> ExecutionBaselineReport: ...

    def get_quality_baseline(
        self,
        *,
        limit: int = 1000,
        status: AgentStatus | None = None,
        city: str | None = None,
        travel_days: int | None = None,
        transportation: str | None = None,
        completion_mode: str | None = None,
        quality_level: str | None = None,
        top_n: int = 20,
        user_id: str | None = None,
    ) -> QualityBaselineReport: ...

    def get_fixed_acceptance_baseline(
        self,
        *,
        limit: int = 5000,
        user_id: str | None = None,
    ) -> FixedAcceptanceBaselineReport: ...


@runtime_checkable
class RouteCacheStore(Protocol):
    """真实路线分段缓存接口。"""

    def get(self, cache_key: str) -> RouteEstimate | None: ...

    def get_entry(self, cache_key: str) -> CacheStoreEntry[RouteEstimate] | None: ...

    def set(
        self,
        cache_key: str,
        estimate: RouteEstimate,
        *,
        ttl_seconds: int,
    ) -> None: ...

    def purge_expired(self) -> int: ...


@runtime_checkable
class RestaurantCacheStore(Protocol):
    """餐饮候选快照缓存接口。"""

    def get(self, cache_key: str) -> RestaurantSearchSnapshot | None: ...

    def get_entry(
        self, cache_key: str
    ) -> CacheStoreEntry[RestaurantSearchSnapshot] | None: ...

    def set(
        self,
        cache_key: str,
        snapshot: RestaurantSearchSnapshot,
        *,
        ttl_seconds: int,
    ) -> None: ...

    def purge_expired(self) -> int: ...


@runtime_checkable
class TripVersionStore(Protocol):
    """行程草稿和正式版本存储接口。"""

    def save_version(self, version: TripPlanVersion) -> TripPlanVersion: ...

    def get_version(self, session_id: str, version_number: int) -> TripPlanVersion: ...

    def get_version_by_id(self, version_id: str) -> TripPlanVersion: ...

    def get_confirmed_version(self, session_id: str) -> TripPlanVersion | None: ...

    def list_versions(self, session_id: str) -> list[TripPlanVersion]: ...

    def next_version_number(self, session_id: str) -> int: ...

    def save_draft(self, draft: TripDraft) -> TripDraft: ...

    def get_draft(self, session_id: str, draft_id: str) -> TripDraft: ...

    def supersede_candidate(self, version_id: str | None) -> None: ...

    def confirm_version(self, version: TripPlanVersion) -> TripPlanVersion: ...


@runtime_checkable
class TripTaskStore(Protocol):
    """异步任务、Worker 租约和可回放进度事件存储接口。"""

    @staticmethod
    def request_fingerprint(request: TripRequest) -> str: ...

    def create_task(
        self,
        request: TripRequest,
        *,
        idempotency_key: str,
        user_id: str | None = None,
    ) -> tuple[TripPlanningTask, bool]: ...

    def get_task(
        self, task_id: str, *, user_id: str | None = None
    ) -> TripPlanningTask: ...

    def list_events(
        self,
        task_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 200,
    ) -> list[TripTaskEvent]: ...

    def assert_worker_owns_task(
        self,
        task_id: str,
        worker_id: str,
    ) -> TripPlanningTask: ...

    def record_progress(
        self,
        task_id: str,
        *,
        worker_id: str,
        event_type: TaskEventType,
        stage: str,
        stage_name: str,
        progress_percent: float,
        current_step: int,
        max_steps: int,
        message: str,
        current_action: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> TripPlanningTask: ...

    def claim_next(
        self,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> TripPlanningTask | None: ...

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float,
    ) -> bool: ...

    def is_cancel_requested(self, task_id: str) -> bool: ...

    def request_cancel(
        self, task_id: str, *, user_id: str | None = None
    ) -> TripPlanningTask: ...

    def mark_succeeded(
        self,
        task_id: str,
        worker_id: str,
        *,
        session_id: str,
    ) -> TripPlanningTask: ...

    def mark_cancelled(
        self,
        task_id: str,
        worker_id: str,
        *,
        message: str,
    ) -> TripPlanningTask: ...

    def mark_failed(
        self,
        task_id: str,
        worker_id: str,
        *,
        report: TaskFailureReport,
        timed_out: bool = False,
    ) -> TripPlanningTask: ...
