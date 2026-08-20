"""统一持久化接口以及 SQLite/MySQL 工厂的回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine

from app.memory import (
    SQLiteAgentStateStore,
    SQLiteRestaurantCache,
    SQLiteRouteCache,
    SQLiteTripVersionStore,
)
from app.persistence import (
    AgentStateStore,
    RestaurantCacheStore,
    RouteCacheStore,
    TripTaskStore,
    TripVersionStore,
    UnsupportedDatabaseBackendError,
    create_persistence_stores,
)
from app.persistence.mysql_agent_state_store import MySQLAgentStateStore
from app.persistence.mysql_restaurant_cache import MySQLRestaurantCache
from app.persistence.mysql_route_cache import MySQLRouteCache
from app.persistence.mysql_trip_task_store import MySQLTripTaskStore
from app.persistence.mysql_trip_version_store import MySQLTripVersionStore
from app.task_runtime import SQLiteTripTaskStore


class PersistenceFactoryTests(unittest.TestCase):
    def test_factory_creates_complete_sqlite_store_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "memory.db"
            stores = create_persistence_stores(
                backend="sqlite",
                sqlite_database_path=database,
            )

            self.assertIsInstance(stores.agent_state_store, SQLiteAgentStateStore)
            self.assertIsInstance(stores.trip_version_store, SQLiteTripVersionStore)
            self.assertIsInstance(stores.trip_task_store, SQLiteTripTaskStore)
            self.assertIsInstance(stores.route_cache, SQLiteRouteCache)
            self.assertIsInstance(stores.restaurant_cache, SQLiteRestaurantCache)

            self.assertIsInstance(stores.agent_state_store, AgentStateStore)
            self.assertIsInstance(stores.trip_version_store, TripVersionStore)
            self.assertIsInstance(stores.trip_task_store, TripTaskStore)
            self.assertIsInstance(stores.route_cache, RouteCacheStore)
            self.assertIsInstance(stores.restaurant_cache, RestaurantCacheStore)

    def test_factory_creates_complete_mysql_store_bundle_with_injected_engine(self) -> None:
        # 工厂本身不访问数据库，因此这里用轻量 Engine 验证注册和依赖注入。
        engine = create_engine("sqlite://")
        stores = create_persistence_stores(
            backend=" MYSQL ",
            sqlite_database_path="unused.db",
            mysql_engine=engine,
        )

        self.assertIsInstance(stores.agent_state_store, MySQLAgentStateStore)
        self.assertIsInstance(stores.trip_version_store, MySQLTripVersionStore)
        self.assertIsInstance(stores.trip_task_store, MySQLTripTaskStore)
        self.assertIsInstance(stores.route_cache, MySQLRouteCache)
        self.assertIsInstance(stores.restaurant_cache, MySQLRestaurantCache)
        self.assertIsInstance(stores.agent_state_store, AgentStateStore)
        self.assertIsInstance(stores.trip_task_store, TripTaskStore)

    def test_factory_can_disable_optional_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stores = create_persistence_stores(
                backend=" SQLITE ",
                sqlite_database_path=Path(directory) / "memory.db",
                route_cache_enabled=False,
                restaurant_cache_enabled=False,
            )

            self.assertIsNone(stores.route_cache)
            self.assertIsNone(stores.restaurant_cache)

    def test_mysql_requires_config_or_injected_engine(self) -> None:
        with self.assertRaisesRegex(ValueError, "mysql_config"):
            create_persistence_stores(
                backend="mysql",
                sqlite_database_path="unused.db",
            )

    def test_factory_rejects_unknown_backend(self) -> None:
        with self.assertRaisesRegex(UnsupportedDatabaseBackendError, "sqlite.*mysql"):
            create_persistence_stores(
                backend="postgresql",
                sqlite_database_path="unused.db",
            )


if __name__ == "__main__":
    unittest.main()
