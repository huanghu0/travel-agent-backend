"""创建本地开发和测试 MySQL 数据库。

凭据只从本地 .env 读取，不接受命令行密码，避免进入 shell 历史。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 支持从项目根目录直接运行脚本，而不要求调用方额外设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from app.core.config import settings
from app.persistence.database import MySQLDatabaseConfig, create_mysql_engine


_SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def _quoted_database(name: str) -> str:
    """只允许安全标识符，并使用反引号包裹数据库名。"""

    if not _SAFE_DATABASE_NAME.fullmatch(name):
        raise ValueError(f"非法数据库名称: {name!r}")
    return f"`{name}`"


def main() -> int:
    parser = argparse.ArgumentParser(description="创建旅行智能体 MySQL 开发/测试数据库")
    parser.add_argument(
        "--database",
        action="append",
        dest="databases",
        help="要创建的数据库；可重复。默认创建 MYSQL_DATABASE 和 MYSQL_TEST_DATABASE",
    )
    args = parser.parse_args()
    databases = args.databases or [settings.MYSQL_DATABASE, settings.MYSQL_TEST_DATABASE]

    config = MySQLDatabaseConfig.from_settings(settings)
    engine = create_mysql_engine(config, include_database=False)
    try:
        with engine.begin() as connection:
            for database in databases:
                quoted = _quoted_database(database)
                connection.execute(
                    text(
                        f"CREATE DATABASE IF NOT EXISTS {quoted} "
                        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                    )
                )
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "status": "ok",
                "server": f"{settings.MYSQL_HOST}:{settings.MYSQL_PORT}",
                "databases": databases,
                "charset": "utf8mb4",
                "collation": "utf8mb4_unicode_ci",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
