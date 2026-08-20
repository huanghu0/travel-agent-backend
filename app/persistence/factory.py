"""根据配置创建一整套持久化 Store。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.persistence.exceptions import UnsupportedDatabaseBackendError
from app.persistence.interfaces import (
    AgentStateStore,
    RestaurantCacheStore,
    RouteCacheStore,
    TripTaskStore,
    TripVersionStore,
)


@dataclass(frozen=True, slots=True)
class PersistenceStores:
    """应用启动时共享的一组数据库后端无关 Store。"""

    agent_state_store: AgentStateStore
    trip_version_store: TripVersionStore
    trip_task_store: TripTaskStore
    route_cache: RouteCacheStore | None
    restaurant_cache: RestaurantCacheStore | None


def create_persistence_stores(
    *,
    backend: str,
    sqlite_database_path: str | Path,
    route_cache_enabled: bool = True,
    restaurant_cache_enabled: bool = True,
) -> PersistenceStores:
    """创建 Store 集合；阶段一只启用 SQLite，MySQL 将在下一阶段注册。"""

    normalized_backend = backend.strip().lower()
    if normalized_backend != "sqlite":
        raise UnsupportedDatabaseBackendError(
            f"暂不支持数据库后端 {backend!r}；当前阶段可用值为 'sqlite'，"
            "MySQL Store 将在下一阶段接入"
        )

    # 延迟导入具体实现，避免业务模块通过 factory 间接形成循环依赖。
    from app.memory.sqlite_restaurant_cache import SQLiteRestaurantCache
    from app.memory.sqlite_route_cache import SQLiteRouteCache
    from app.memory.sqlite_store import SQLiteAgentStateStore
    from app.memory.sqlite_trip_version_store import SQLiteTripVersionStore
    from app.task_runtime.store import SQLiteTripTaskStore

    database_path = Path(sqlite_database_path).expanduser()
    return PersistenceStores(
        agent_state_store=SQLiteAgentStateStore(database_path),
        trip_version_store=SQLiteTripVersionStore(database_path),
        trip_task_store=SQLiteTripTaskStore(database_path),
        route_cache=(
            SQLiteRouteCache(database_path) if route_cache_enabled else None
        ),
        restaurant_cache=(
            SQLiteRestaurantCache(database_path)
            if restaurant_cache_enabled
            else None
        ),
    )
