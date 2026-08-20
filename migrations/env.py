"""Alembic 环境：从本地 .env 构建 MySQL URL，不在仓库保存凭据。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.persistence.database import MySQLDatabaseConfig
from app.persistence.sqlalchemy_models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `alembic -x database=travel_agent_test upgrade head` 可显式选择测试库。
x_arguments = context.get_x_argument(as_dictionary=True)
database_name = x_arguments.get("database", settings.MYSQL_DATABASE)
mysql_config = MySQLDatabaseConfig.from_settings(settings, database=database_name)
# ConfigParser 会解释百分号，因此必须转义 URL 中可能出现的 `%`。
database_url = mysql_config.sqlalchemy_url().render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线生成 SQL，不创建数据库连接。"""

    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线执行迁移，连接参数来自项目 MySQL 配置。"""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={
            "connect_timeout": settings.MYSQL_CONNECT_TIMEOUT_SECONDS,
            "read_timeout": settings.MYSQL_READ_TIMEOUT_SECONDS,
            "write_timeout": settings.MYSQL_WRITE_TIMEOUT_SECONDS,
        },
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
