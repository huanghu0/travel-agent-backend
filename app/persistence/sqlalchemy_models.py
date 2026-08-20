"""MySQL 七张业务表的 SQLAlchemy 元数据。

下一阶段 MySQL Store 将继续使用 Pydantic JSON 字符串，因此大型快照采用 LONGTEXT，
避免普通 TEXT 的 64 KiB 上限，也避免迁移时改变现有 model_validate_json 语义。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _long_text():
    return Text().with_variant(LONGTEXT(), "mysql")


def _datetime_utc():
    """所有后端使用无时区 datetime，业务层统一按 UTC 读写。"""

    return DateTime().with_variant(DATETIME(fsp=6), "mysql")


MYSQL_TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class AgentSessionRow(Base):
    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    action_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state_json: Mapped[str] = mapped_column(_long_text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)
    travel_days: Mapped[int | None] = mapped_column(Integer)
    transportation: Mapped[str | None] = mapped_column(String(64))
    completion_mode: Mapped[str | None] = mapped_column(String(32))
    quality_level: Mapped[str | None] = mapped_column(String(32))
    quality_score: Mapped[float | None] = mapped_column(Float)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    issue_codes_json: Mapped[str] = mapped_column(
        _long_text(), nullable=False
    )
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    llm_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("idx_agent_sessions_updated_at", "updated_at"),
        Index("idx_agent_sessions_status", "status"),
        Index("idx_agent_sessions_city", "city"),
        Index("idx_agent_sessions_completion_mode", "completion_mode"),
        Index("idx_agent_sessions_quality_level", "quality_level"),
        Index(
            "idx_agent_sessions_request_dimensions",
            "city",
            "travel_days",
            "transportation",
        ),
        MYSQL_TABLE_ARGS,
    )


class RouteCacheRow(Base):
    __tablename__ = "route_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    estimate_json: Mapped[str] = mapped_column(_long_text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)

    __table_args__ = (
        Index("idx_route_cache_expires_at", "expires_at"),
        MYSQL_TABLE_ARGS,
    )


class RestaurantCacheRow(Base):
    __tablename__ = "restaurant_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    keywords: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(_long_text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)

    __table_args__ = (
        Index("idx_restaurant_cache_expires_at", "expires_at"),
        MYSQL_TABLE_ARGS,
    )


class TripPlanVersionRow(Base):
    __tablename__ = "trip_plan_versions"

    version_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_draft_id: Mapped[str | None] = mapped_column(String(36))
    version_json: Mapped[str] = mapped_column(_long_text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(_datetime_utc())

    __table_args__ = (
        UniqueConstraint("session_id", "version_number", name="uq_trip_versions_session_number"),
        Index("idx_trip_versions_session", "session_id", "version_number"),
        MYSQL_TABLE_ARGS,
    )


class TripDraftRow(Base):
    __tablename__ = "trip_drafts"

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    base_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    draft_json: Mapped[str] = mapped_column(_long_text(), nullable=False)
    candidate_version_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)

    __table_args__ = (
        Index("idx_trip_drafts_session", "session_id", "updated_at"),
        MYSQL_TABLE_ARGS,
    )


class TripPlanningTaskRow(Base):
    __tablename__ = "trip_planning_tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean(create_constraint=False), nullable=False, server_default=text("0")
    )
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(_datetime_utc())
    heartbeat_at: Mapped[datetime | None] = mapped_column(_datetime_utc())
    task_json: Mapped[str] = mapped_column(_long_text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)

    __table_args__ = (
        Index("idx_trip_tasks_status_created", "status", "created_at"),
        Index("idx_trip_tasks_lease", "lease_expires_at"),
        Index("idx_trip_tasks_fingerprint", "request_fingerprint", "status", "created_at"),
        MYSQL_TABLE_ARGS,
    )


class TripTaskEventRow(Base):
    __tablename__ = "trip_task_events"

    event_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("trip_planning_tasks.task_id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_json: Mapped[str] = mapped_column(_long_text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_datetime_utc(), nullable=False)

    __table_args__ = (
        Index("idx_trip_task_events_task_id", "task_id", "event_id"),
        MYSQL_TABLE_ARGS,
    )


