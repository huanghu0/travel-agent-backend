"""统一持久化接口、工厂和数据库无关异常。"""

from app.persistence.database import (
    DatabaseHealth,
    MySQLDatabaseConfig,
    check_mysql_health,
    create_mysql_engine,
)
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
from app.persistence.schema_validation import (
    MySQLSchemaValidation,
    validate_mysql_schema,
)
from app.persistence.interfaces import (
    AgentStateStore,
    RestaurantCacheStore,
    RouteCacheStore,
    TripTaskStore,
    TripVersionStore,
)

__all__ = [
    "AgentStateStore",
    "DatabaseHealth",
    "MySQLDatabaseConfig",
    "MySQLSchemaValidation",
    "check_mysql_health",
    "create_mysql_engine",
    "validate_mysql_schema",
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
