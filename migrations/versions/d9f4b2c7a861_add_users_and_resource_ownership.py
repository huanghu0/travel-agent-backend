"""增加用户认证和旅行资源所有权。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "d9f4b2c7a861"
down_revision = "a31f0c8d4b72"
branch_labels = None
depends_on = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        **TABLE_OPTIONS,
    )

    op.add_column("agent_sessions", sa.Column("user_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_agent_sessions_user_id_users",
        "agent_sessions",
        "users",
        ["user_id"],
        ["user_id"],
    )
    op.create_index(
        "idx_agent_sessions_user_updated", "agent_sessions", ["user_id", "updated_at"]
    )
    op.create_index(
        "idx_agent_sessions_user_status_updated",
        "agent_sessions",
        ["user_id", "status", "updated_at"],
    )

    op.add_column(
        "trip_planning_tasks", sa.Column("user_id", sa.String(length=36), nullable=True)
    )
    op.create_foreign_key(
        "fk_trip_planning_tasks_user_id_users",
        "trip_planning_tasks",
        "users",
        ["user_id"],
        ["user_id"],
    )
    op.create_index(
        "idx_trip_tasks_user_created", "trip_planning_tasks", ["user_id", "created_at"]
    )
    op.create_index(
        "idx_trip_tasks_user_status_created",
        "trip_planning_tasks",
        ["user_id", "status", "created_at"],
    )
    op.drop_constraint(
        "uq_trip_planning_tasks_idempotency_key",
        "trip_planning_tasks",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_trip_tasks_user_idempotency",
        "trip_planning_tasks",
        ["user_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_trip_tasks_user_idempotency", "trip_planning_tasks", type_="unique"
    )
    op.create_unique_constraint(
        "uq_trip_planning_tasks_idempotency_key",
        "trip_planning_tasks",
        ["idempotency_key"],
    )
    op.drop_index("idx_trip_tasks_user_status_created", table_name="trip_planning_tasks")
    op.drop_index("idx_trip_tasks_user_created", table_name="trip_planning_tasks")
    op.drop_constraint(
        "fk_trip_planning_tasks_user_id_users", "trip_planning_tasks", type_="foreignkey"
    )
    op.drop_column("trip_planning_tasks", "user_id")

    op.drop_index("idx_agent_sessions_user_status_updated", table_name="agent_sessions")
    op.drop_index("idx_agent_sessions_user_updated", table_name="agent_sessions")
    op.drop_constraint("fk_agent_sessions_user_id_users", "agent_sessions", type_="foreignkey")
    op.drop_column("agent_sessions", "user_id")
    op.drop_table("users")
