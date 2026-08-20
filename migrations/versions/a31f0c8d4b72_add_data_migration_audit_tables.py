"""增加 SQLite -> MySQL 历史数据迁移审计表。"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "a31f0c8d4b72"
down_revision = "79714a229219"
branch_labels = None
depends_on = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def upgrade() -> None:
    """创建迁移批次和逐行回滚凭证表。"""

    op.create_table(
        "data_migration_batches",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("source_path", mysql.LONGTEXT(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_size_bytes", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("backup_path", mysql.LONGTEXT(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("error_text", mysql.LONGTEXT(), nullable=True),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.PrimaryKeyConstraint("batch_id", name="pk_data_migration_batches"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "idx_data_migration_batches_status_started",
        "data_migration_batches",
        ["status", "started_at"],
    )

    op.create_table(
        "data_migration_records",
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("table_name", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("target_key_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("row_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["data_migration_batches.batch_id"],
            name="fk_data_migration_records_batch_id_data_migration_batches",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "batch_id",
            "table_name",
            "source_key",
            name="pk_data_migration_records",
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "idx_data_migration_records_batch_table",
        "data_migration_records",
        ["batch_id", "table_name"],
    )


def downgrade() -> None:
    """先删除逐行凭证，再删除迁移批次。"""

    op.drop_table("data_migration_records")
    op.drop_index(
        "idx_data_migration_batches_status_started",
        table_name="data_migration_batches",
    )
    op.drop_table("data_migration_batches")
