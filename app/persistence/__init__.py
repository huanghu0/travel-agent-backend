"""统一持久化接口、工厂和数据库无关异常。"""

from app.persistence.exceptions import (
    DraftConflictError,
    DraftNotFoundError,
    SessionNotFoundError,
    TaskIdempotencyConflictError,
    TaskLeaseLostError,
    TripTaskNotFoundError,
    UnsupportedDatabaseBackendError,
    VersionNotFoundError,
)
from app.persistence.factory import PersistenceStores, create_persistence_stores
from app.persistence.interfaces import (
    AgentStateStore,
    RestaurantCacheStore,
    RouteCacheStore,
    TripTaskStore,
    TripVersionStore,
)

__all__ = [
    "AgentStateStore",
    "DraftConflictError",
    "DraftNotFoundError",
    "PersistenceStores",
    "RestaurantCacheStore",
    "RouteCacheStore",
    "SessionNotFoundError",
    "TaskIdempotencyConflictError",
    "TaskLeaseLostError",
    "TripTaskNotFoundError",
    "TripTaskStore",
    "TripVersionStore",
    "UnsupportedDatabaseBackendError",
    "VersionNotFoundError",
    "create_persistence_stores",
]
