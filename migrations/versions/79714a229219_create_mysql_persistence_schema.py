"""create mysql persistence schema

Revision ID: 79714a229219
Revises:
Create Date: 2026-08-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "79714a229219"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    """创建与现有 SQLite 数据范围对应的七张 MySQL 表。"""

    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("state_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("travel_days", sa.Integer(), nullable=True),
        sa.Column("transportation", sa.String(length=64), nullable=True),
        sa.Column("completion_mode", sa.String(length=32), nullable=True),
        sa.Column("quality_level", sa.String(length=32), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("warning_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("issue_codes_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("tool_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("llm_call_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_duration_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name="pk_agent_sessions"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_agent_sessions_updated_at", "agent_sessions", ["updated_at"])
    op.create_index("idx_agent_sessions_status", "agent_sessions", ["status"])
    op.create_index("idx_agent_sessions_city", "agent_sessions", ["city"])
    op.create_index("idx_agent_sessions_completion_mode", "agent_sessions", ["completion_mode"])
    op.create_index("idx_agent_sessions_quality_level", "agent_sessions", ["quality_level"])
    op.create_index(
        "idx_agent_sessions_request_dimensions",
        "agent_sessions",
        ["city", "travel_days", "transportation"],
    )

    op.create_table(
        "route_cache",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("estimate_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("cache_key", name="pk_route_cache"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_route_cache_expires_at", "route_cache", ["expires_at"])

    op.create_table(
        "restaurant_cache",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=False),
        sa.Column("keywords", sa.String(length=255), nullable=False),
        sa.Column("snapshot_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("cache_key", name="pk_restaurant_cache"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_restaurant_cache_expires_at", "restaurant_cache", ["expires_at"])

    op.create_table(
        "trip_plan_versions",
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_draft_id", sa.String(length=36), nullable=True),
        sa.Column("version_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("confirmed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("version_id", name="pk_trip_plan_versions"),
        sa.UniqueConstraint("session_id", "version_number", name="uq_trip_versions_session_number"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "idx_trip_versions_session",
        "trip_plan_versions",
        ["session_id", "version_number"],
    )

    op.create_table(
        "trip_drafts",
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("draft_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("candidate_version_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("draft_id", name="pk_trip_drafts"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_trip_drafts_session", "trip_drafts", ["session_id", "updated_at"])

    op.create_table(
        "trip_planning_tasks",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("heartbeat_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("task_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("task_id", name="pk_trip_planning_tasks"),
        sa.UniqueConstraint("session_id", name="uq_trip_planning_tasks_session_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_trip_planning_tasks_idempotency_key"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "idx_trip_tasks_status_created",
        "trip_planning_tasks",
        ["status", "created_at"],
    )
    op.create_index("idx_trip_tasks_lease", "trip_planning_tasks", ["lease_expires_at"])
    op.create_index(
        "idx_trip_tasks_fingerprint",
        "trip_planning_tasks",
        ["request_fingerprint", "status", "created_at"],
    )

    op.create_table(
        "trip_task_events",
        sa.Column("event_id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["trip_planning_tasks.task_id"],
            name="fk_trip_task_events_task_id_trip_planning_tasks",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_trip_task_events"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "idx_trip_task_events_task_id",
        "trip_task_events",
        ["task_id", "event_id"],
    )


def downgrade() -> None:
    """按依赖关系逆序删除全部 MySQL 业务表。"""

    # 该索引同时支撑外键；直接删表可由 MySQL 一并安全移除索引和外键。
    op.drop_table("trip_task_events")

    op.drop_index("idx_trip_tasks_fingerprint", table_name="trip_planning_tasks")
    op.drop_index("idx_trip_tasks_lease", table_name="trip_planning_tasks")
    op.drop_index("idx_trip_tasks_status_created", table_name="trip_planning_tasks")
    op.drop_table("trip_planning_tasks")

    op.drop_index("idx_trip_drafts_session", table_name="trip_drafts")
    op.drop_table("trip_drafts")

    op.drop_index("idx_trip_versions_session", table_name="trip_plan_versions")
    op.drop_table("trip_plan_versions")

    op.drop_index("idx_restaurant_cache_expires_at", table_name="restaurant_cache")
    op.drop_table("restaurant_cache")

    op.drop_index("idx_route_cache_expires_at", table_name="route_cache")
    op.drop_table("route_cache")

    op.drop_index("idx_agent_sessions_request_dimensions", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_quality_level", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_completion_mode", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_city", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_status", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_updated_at", table_name="agent_sessions")
    op.drop_table("agent_sessions")

