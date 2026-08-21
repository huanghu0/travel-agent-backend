"""前端读取模型的 Redis 快照缓存；持久化 Store 始终是事实来源。"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.infrastructure.cache import CacheReadStatus, CacheStore
from app.infrastructure.redis.keys import RedisKeyBuilder


TModel = TypeVar("TModel", bound=BaseModel)


class ReadModelSnapshotCache:
    """缓存 execution-view 和任务进度，损坏或不可用时透明回退数据库。"""

    def __init__(
        self,
        cache_store: CacheStore,
        key_builder: RedisKeyBuilder,
        *,
        execution_view_ttl_seconds: int,
        task_active_ttl_seconds: int,
        task_terminal_ttl_seconds: int,
    ) -> None:
        self.cache_store = cache_store
        self.key_builder = key_builder
        self.execution_view_ttl_seconds = execution_view_ttl_seconds
        self.task_active_ttl_seconds = task_active_ttl_seconds
        self.task_terminal_ttl_seconds = task_terminal_ttl_seconds

    def get_execution_view(self, session_id: str, model_type: type[TModel]) -> TModel | None:
        return self._get(self.key_builder.execution_view(session_id), model_type)

    def set_execution_view(self, session_id: str, value: BaseModel, *, active: bool) -> None:
        ttl = min(self.execution_view_ttl_seconds, 5) if active else self.execution_view_ttl_seconds
        self._set(self.key_builder.execution_view(session_id), value, ttl)

    def delete_execution_view(self, session_id: str) -> None:
        self._delete(self.key_builder.execution_view(session_id))

    def get_task_progress(self, task_id: str, model_type: type[TModel]) -> TModel | None:
        return self._get(self.key_builder.task_progress(task_id), model_type)

    def set_task_progress(self, task_id: str, value: BaseModel, *, terminal: bool) -> None:
        ttl = self.task_terminal_ttl_seconds if terminal else self.task_active_ttl_seconds
        self._set(self.key_builder.task_progress(task_id), value, ttl)

    def _get(self, key: str, model_type: type[TModel]) -> TModel | None:
        try:
            lookup = self.cache_store.get(key)
            if lookup.status != CacheReadStatus.HIT:
                return None
            return model_type.model_validate(lookup.value)
        except (ValidationError, TypeError, ValueError):
            self._delete(key)
            return None
        except Exception:
            return None

    def _set(self, key: str, value: BaseModel, ttl_seconds: int) -> None:
        try:
            self.cache_store.set(
                key,
                value.model_dump(mode="json"),
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            pass

    def _delete(self, key: str) -> None:
        try:
            self.cache_store.delete(key)
        except Exception:
            pass
