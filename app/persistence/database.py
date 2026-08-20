"""MySQL 连接配置、SQLAlchemy 引擎和只读健康检查。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, quote_plus

from sqlalchemy import Engine, URL, create_engine, text


@dataclass(frozen=True, slots=True)
class MySQLDatabaseConfig:
    """创建 MySQL 引擎所需的最小配置，不在日志中输出密码。"""

    host: str = "127.0.0.1"
    port: int = 3306
    database: str = "travel_agent"
    user: str = "root"
    password: str = ""
    charset: str = "utf8mb4"
    pool_size: int = 10
    max_overflow: int = 20
    pool_recycle_seconds: int = 1800
    pool_pre_ping: bool = True
    connect_timeout_seconds: int = 5
    read_timeout_seconds: int = 30
    write_timeout_seconds: int = 30

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        database: str | None = None,
    ) -> "MySQLDatabaseConfig":
        """从项目 Settings 构建配置，测试时也可以传入轻量替身。"""

        return cls(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            database=database or settings.MYSQL_DATABASE,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD or "",
            charset=settings.MYSQL_CHARSET,
            pool_size=settings.MYSQL_POOL_SIZE,
            max_overflow=settings.MYSQL_MAX_OVERFLOW,
            pool_recycle_seconds=settings.MYSQL_POOL_RECYCLE_SECONDS,
            pool_pre_ping=settings.MYSQL_POOL_PRE_PING,
            connect_timeout_seconds=settings.MYSQL_CONNECT_TIMEOUT_SECONDS,
            read_timeout_seconds=settings.MYSQL_READ_TIMEOUT_SECONDS,
            write_timeout_seconds=settings.MYSQL_WRITE_TIMEOUT_SECONDS,
        )

    def sqlalchemy_url(self, *, include_database: bool = True) -> URL:
        """使用 SQLAlchemy URL 对特殊字符转义，避免手工拼接密码。"""

        return URL.create(
            drivername="mysql+pymysql",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.database if include_database else None,
            query={"charset": self.charset},
        )

    def safe_target(self) -> str:
        """返回不包含用户名和密码的诊断目标。"""

        return f"{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """MySQL 健康检查结果；不携带连接密码。"""

    healthy: bool
    target: str
    database: str | None = None
    server_version: str | None = None
    error: str | None = None


def create_mysql_engine(
    config: MySQLDatabaseConfig,
    *,
    include_database: bool = True,
) -> Engine:
    """创建线程安全的 SQLAlchemy 连接池。

    ``pool_pre_ping`` 用于丢弃断开的连接；``pool_recycle`` 避免连接超过
    MySQL ``wait_timeout`` 后仍留在连接池。应用层时间统一按 UTC 写入
    ``DATETIME(6)``。
    """

    return create_engine(
        config.sqlalchemy_url(include_database=include_database),
        pool_size=config.pool_size,
        max_overflow=config.max_overflow,
        pool_recycle=config.pool_recycle_seconds,
        pool_pre_ping=config.pool_pre_ping,
        isolation_level="READ COMMITTED",
        connect_args={
            "connect_timeout": config.connect_timeout_seconds,
            "read_timeout": config.read_timeout_seconds,
            "write_timeout": config.write_timeout_seconds,
        },
    )


def _safe_error_message(exc: Exception, config: MySQLDatabaseConfig) -> str:
    """压平异常并擦除原始/URL 编码密码，避免健康检查泄露凭据。"""

    message = " ".join(str(exc).split()) or exc.__class__.__name__
    if config.password:
        password_variants = {
            config.password,
            quote(config.password, safe=""),
            quote_plus(config.password, safe=""),
        }
        for password in sorted(password_variants, key=len, reverse=True):
            if password:
                message = message.replace(password, "***")
    return message[:500]


def check_mysql_health(engine: Engine, config: MySQLDatabaseConfig) -> DatabaseHealth:
    """执行只读查询，验证连接、目标数据库和服务端版本。"""

    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT DATABASE() AS database_name, VERSION() AS server_version")
            ).mappings().one()
        return DatabaseHealth(
            healthy=True,
            target=config.safe_target(),
            database=row["database_name"],
            server_version=row["server_version"],
        )
    except Exception as exc:
        return DatabaseHealth(
            healthy=False,
            target=config.safe_target(),
            error=_safe_error_message(exc, config),
        )
