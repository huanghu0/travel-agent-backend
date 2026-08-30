"""add shared guides and rag jobs"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "f4c2a81d9e30"
down_revision = "d9f4b2c7a861"
branch_labels = None
depends_on = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    op.create_table(
        "shared_guides",
        sa.Column("share_id", sa.String(length=36), nullable=False),
        sa.Column("author_user_id", sa.String(length=36), nullable=False),
        sa.Column("source_session_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=False),
        sa.Column("city_normalized", sa.String(length=128), nullable=False),
        sa.Column("travel_days", sa.Integer(), nullable=False),
        sa.Column("transportation", sa.String(length=64), nullable=False),
        sa.Column("accommodation", sa.String(length=128), nullable=False),
        sa.Column("preferences_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("snapshot_json", mysql.LONGTEXT(), nullable=False),
        sa.Column("retrieval_text", mysql.LONGTEXT(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("quality_level", sa.String(length=32), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("publication_status", sa.String(length=32), nullable=False),
        sa.Column("index_status", sa.String(length=32), nullable=False),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("retrieval_template_version", sa.String(length=64), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("like_count", mysql.BIGINT(unsigned=True), nullable=False, server_default=sa.text("0")),
        sa.Column("last_index_error", mysql.LONGTEXT(), nullable=True),
        sa.Column("indexed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("published_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.user_id"],
            name="fk_shared_guides_author_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("share_id", name="pk_shared_guides"),
        sa.UniqueConstraint("author_user_id", "source_session_id", name="uq_shared_guides_author_session"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_shared_guides_author_updated", "shared_guides", ["author_user_id", "updated_at"])
    op.create_index("idx_shared_guides_public_published", "shared_guides", ["publication_status", "published_at"])
    op.create_index("idx_shared_guides_public_city_days_published", "shared_guides", ["publication_status", "city_normalized", "travel_days", "published_at"])
    op.create_index("idx_shared_guides_public_likes", "shared_guides", ["publication_status", "like_count", "share_id"])
    op.create_index("idx_shared_guides_session_author", "shared_guides", ["source_session_id", "author_user_id"])
    op.create_index("idx_shared_guides_index_updated", "shared_guides", ["index_status", "updated_at"])

    op.create_table(
        "shared_guide_likes",
        sa.Column("like_id", sa.String(length=36), nullable=False),
        sa.Column("share_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["share_id"], ["shared_guides.share_id"], name="fk_shared_guide_likes_share_id_shared_guides", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], name="fk_shared_guide_likes_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("like_id", name="pk_shared_guide_likes"),
        sa.UniqueConstraint("share_id", "user_id", name="uq_shared_guide_likes_share_user"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "share_index_jobs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("share_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_retry_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "lease_owner",
            mysql.VARCHAR(length=128, charset="utf8mb4", collation="utf8mb4_bin"),
            nullable=True,
        ),
        sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error", mysql.LONGTEXT(), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(["share_id"], ["shared_guides.share_id"], name="fk_share_index_jobs_share_id_shared_guides", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", name="pk_share_index_jobs"),
        sa.UniqueConstraint("share_id", "operation", "index_version", name="uq_share_index_jobs_version_operation"),
        **TABLE_OPTIONS,
    )
    op.create_index("idx_share_index_jobs_status_retry", "share_index_jobs", ["status", "next_retry_at"])
    op.create_index("idx_share_index_jobs_lease", "share_index_jobs", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_table("share_index_jobs")
    op.drop_table("shared_guide_likes")
    op.drop_table("shared_guides")
