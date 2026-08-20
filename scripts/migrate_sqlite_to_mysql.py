"""SQLite -> MySQL 历史数据迁移命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 支持从项目根目录直接运行，不要求调用方设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from app.core.config import settings
from app.persistence.database import MySQLDatabaseConfig, create_mysql_engine
from app.persistence.sqlite_backup import create_sqlite_backup
from app.persistence.sqlite_mysql_migration import (
    BATCH_TABLE,
    SQLiteMySQLMigrationError,
    SQLiteToMySQLMigrator,
)


def _json_print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _batch_snapshot(migrator: SQLiteToMySQLMigrator, batch_id: str) -> Path:
    """从批次审计记录定位 execute 使用的一致性快照。"""

    with migrator.engine.connect() as connection:
        row = connection.execute(
            select(BATCH_TABLE).where(BATCH_TABLE.c.batch_id == batch_id)
        ).mappings().first()
    if row is None:
        raise SQLiteMySQLMigrationError(f"迁移批次不存在: {batch_id}")
    raw_path = row["backup_path"]
    if not raw_path:
        raise SQLiteMySQLMigrationError(
            "该批次没有 backup_path，请通过 --snapshot 显式提供原迁移快照"
        )
    return Path(raw_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将 SQLite 会话、缓存、版本和异步任务历史迁移到 MySQL"
    )
    parser.add_argument(
        "--database",
        default=settings.MYSQL_DATABASE,
        help="目标 MySQL 数据库名，默认读取 MYSQL_DATABASE",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    dry_run = subparsers.add_parser("dry-run", help="只读校验源数据和目标冲突")
    dry_run.add_argument("--source", type=Path, default=Path(settings.AGENT_MEMORY_DB_PATH))

    execute = subparsers.add_parser("execute", help="创建快照并迁移缺失数据")
    execute.add_argument("--source", type=Path, default=Path(settings.AGENT_MEMORY_DB_PATH))
    execute.add_argument("--backup-dir", type=Path, default=Path("data/backups"))
    execute.add_argument(
        "--snapshot",
        type=Path,
        help="使用已有不可变快照；恢复批次时必须与原批次 SHA-256 相同",
    )
    execute.add_argument("--resume-batch-id", help="恢复 running/failed 批次")
    execute.add_argument(
        "--allow-conflicts",
        action="store_true",
        help="保留 MySQL 现有冲突行并完成批次；verify 仍会阻止切换",
    )

    verify = subparsers.add_parser("verify", help="逐行验证源快照和 MySQL")
    verify.add_argument("--batch-id", help="验证指定批次并更新其状态")
    verify.add_argument("--snapshot", type=Path, help="显式指定原迁移快照")
    verify.add_argument("--source", type=Path, default=Path(settings.AGENT_MEMORY_DB_PATH))

    rollback = subparsers.add_parser("rollback", help="安全回滚指定批次插入的数据")
    rollback.add_argument("--batch-id", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = MySQLDatabaseConfig.from_settings(settings, database=args.database)
    engine = create_mysql_engine(config)
    migrator = SQLiteToMySQLMigrator(engine)
    try:
        if args.command == "dry-run":
            result = migrator.dry_run(args.source)
            _json_print(result)
            return 0 if result["valid"] and not result["has_conflicts"] else 2

        if args.command == "execute":
            if args.resume_batch_id and args.snapshot is None:
                snapshot = _batch_snapshot(migrator, args.resume_batch_id)
                manifest = None
            elif args.snapshot is not None:
                snapshot = args.snapshot
                manifest = None
            else:
                snapshot, manifest = create_sqlite_backup(args.source, args.backup_dir)
            result = migrator.execute(
                snapshot,
                original_source_path=args.source,
                backup_path=snapshot,
                resume_batch_id=args.resume_batch_id,
                fail_on_conflict=not args.allow_conflicts,
            )
            if manifest:
                result["backup_manifest"] = str(manifest.resolve())
            _json_print(result)
            return 0

        if args.command == "verify":
            snapshot = args.snapshot
            if snapshot is None and args.batch_id:
                snapshot = _batch_snapshot(migrator, args.batch_id)
            result = migrator.verify(snapshot or args.source, batch_id=args.batch_id)
            _json_print(result)
            return 0 if result["verified"] else 3

        result = migrator.rollback(args.batch_id)
        _json_print(result)
        return 0 if result["fully_rolled_back"] else 4
    except Exception as exc:
        error_payload = {
            "status": "error",
            "error_type": exc.__class__.__name__,
            "message": str(exc),
            "target": config.safe_target(),
        }
        if getattr(exc, "batch_id", None):
            error_payload["batch_id"] = exc.batch_id
        _json_print(error_payload)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
