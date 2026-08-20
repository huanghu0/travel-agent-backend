"""统一持久化接口和 SQLite 工厂的回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

            # runtime_checkable Protocol 验证具体 Store 对统一契约的完整实现。
            self.assertIsInstance(stores.agent_state_store, AgentStateStore)
            self.assertIsInstance(stores.trip_version_store, TripVersionStore)
            self.assertIsInstance(stores.trip_task_store, TripTaskStore)
            self.assertIsInstance(stores.route_cache, RouteCacheStore)
            self.assertIsInstance(stores.restaurant_cache, RestaurantCacheStore)

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

    def test_factory_rejects_unregistered_backend(self) -> None:
        with self.assertRaisesRegex(
            UnsupportedDatabaseBackendError,
            "MySQL Store 将在下一阶段接入",
        ):
            create_persistence_stores(
                backend="mysql",
                sqlite_database_path="unused.db",
            )


if __name__ == "__main__":
    unittest.main()
