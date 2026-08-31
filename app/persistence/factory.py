"""根据配置创建一整套持久化 Store。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine

from app.persistence.database import MySQLDatabaseConfig, create_mysql_engine
from app.persistence.exceptions import UnsupportedDatabaseBackendError
from app.persistence.interfaces import (
    AgentStateStore,
    RestaurantCacheStore,
    RouteCacheStore,
    TripTaskStore,
    TripVersionStore,
)
from app.sharing.store import SharedGuideStore

if TYPE_CHECKING:
    from app.auth.service import UserStore


@dataclass(frozen=True, slots=True)
class PersistenceStores:
    """应用启动时共享的一组数据库后端无关 Store。"""

    agent_state_store: AgentStateStore
    trip_version_store: TripVersionStore
    trip_task_store: TripTaskStore
    route_cache: RouteCacheStore | None
    restaurant_cache: RestaurantCacheStore | None
    user_store: UserStore | None = None
    shared_guide_store: SharedGuideStore | None = None


def create_persistence_stores(
    *,
    backend: str,
    sqlite_database_path: str | Path,
    mysql_config: MySQLDatabaseConfig | None = None,
    mysql_engine: Engine | None = None,
    route_cache_enabled: bool = True,
    restaurant_cache_enabled: bool = True,
) -> PersistenceStores:
    """按配置创建完整 Store 集合；测试可注入已创建的 MySQL Engine。"""

    normalized_backend = backend.strip().lower()
    if normalized_backend == "sqlite":
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
            route_cache=(SQLiteRouteCache(database_path) if route_cache_enabled else None),
            restaurant_cache=(
                SQLiteRestaurantCache(database_path) if restaurant_cache_enabled else None
            ),
            user_store=None,
            shared_guide_store=None,
        )

    if normalized_backend == "mysql":
        from app.persistence.mysql_agent_state_store import MySQLAgentStateStore
        from app.persistence.mysql_restaurant_cache import MySQLRestaurantCache
        from app.persistence.mysql_route_cache import MySQLRouteCache
        from app.persistence.mysql_trip_task_store import MySQLTripTaskStore
        from app.persistence.mysql_trip_version_store import MySQLTripVersionStore
        from app.auth.store import MySQLUserStore
        from app.sharing.mysql_store import MySQLSharedGuideStore

        engine = mysql_engine
        if engine is None:
            if mysql_config is None:
                raise ValueError("DATABASE_BACKEND=mysql 时必须提供 mysql_config 或 mysql_engine")
            engine = create_mysql_engine(mysql_config)
        return PersistenceStores(
            agent_state_store=MySQLAgentStateStore(engine),
            trip_version_store=MySQLTripVersionStore(engine),
            trip_task_store=MySQLTripTaskStore(engine),
            route_cache=MySQLRouteCache(engine) if route_cache_enabled else None,
            restaurant_cache=(
                MySQLRestaurantCache(engine) if restaurant_cache_enabled else None
            ),
            user_store=MySQLUserStore(engine),
            shared_guide_store=MySQLSharedGuideStore(engine),
        )

    raise UnsupportedDatabaseBackendError(
        f"不支持数据库后端 {backend!r}；可用值为 'sqlite' 或 'mysql'"
    )
