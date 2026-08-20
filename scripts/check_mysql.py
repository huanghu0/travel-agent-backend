"""从本地 .env 检查 MySQL 连接、Alembic 版本和物理 Schema。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 支持从项目根目录直接运行脚本，而不要求调用方额外设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import inspect, text

from app.core.config import settings
from app.persistence.database import (
    MySQLDatabaseConfig,
    check_mysql_health,
    create_mysql_engine,
)
from app.persistence.schema_validation import validate_mysql_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="检查旅行智能体 MySQL 基础设施")
    parser.add_argument(
        "--database",
        default=None,
        help="覆盖 MYSQL_DATABASE，例如 travel_agent_test",
    )
    args = parser.parse_args()

    config = MySQLDatabaseConfig.from_settings(settings, database=args.database)
    engine = create_mysql_engine(config)
    try:
        health = check_mysql_health(engine, config)
        tables: list[str] = []
        revision = None
        schema_valid = False
        schema_errors: list[str] = []
        if health.healthy:
            inspector = inspect(engine)
            tables = sorted(inspector.get_table_names())
            if "alembic_version" in tables:
                with engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version LIMIT 1")
                    ).scalar_one_or_none()
            schema = validate_mysql_schema(engine)
            schema_valid = schema.valid
            schema_errors = schema.errors
        payload = {
            "healthy": health.healthy,
            "target": health.target,
            "database": health.database,
            "server_version": health.server_version,
            "alembic_revision": revision,
            "schema_valid": schema_valid,
            "schema_errors": schema_errors,
            "tables": tables,
            "error": health.error,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if health.healthy and schema_valid else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
