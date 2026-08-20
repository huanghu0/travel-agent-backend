"""MySQL Store 共用的 UTC 时间和 SQLAlchemy 辅助函数。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Engine


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，供业务模型和租约比较使用。"""

    return datetime.now(timezone.utc)


def mysql_utc(value: datetime | None) -> datetime | None:
    """把带时区时间转换为 MySQL ``DATETIME(6)`` 使用的无时区 UTC。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """把 MySQL 返回的无时区 ``DATETIME`` 恢复为带 UTC 时区时间。"""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class MySQLStoreBase:
    """所有 MySQL Store 共享同一个线程安全 SQLAlchemy Engine。"""

    def __init__(self, engine: Engine):
        self.engine = engine
