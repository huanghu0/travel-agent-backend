"""使用 SQLite Online Backup API 创建一致性备份并输出校验清单。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 支持从项目根目录直接运行，不要求调用方设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.persistence.sqlite_backup import create_sqlite_backup

# 保留旧函数名，避免已有自动化或测试导入路径失效。
create_backup = create_sqlite_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="创建一致性 SQLite 备份")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("data/agent_memory.db"),
        help="源 SQLite 数据库，默认 data/agent_memory.db",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/backups"),
        help="备份目录，默认 data/backups",
    )
    args = parser.parse_args()

    backup, manifest = create_sqlite_backup(args.source, args.output_dir)
    print(
        json.dumps(
            {"backup": str(backup), "manifest": str(manifest), "status": "ok"},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
