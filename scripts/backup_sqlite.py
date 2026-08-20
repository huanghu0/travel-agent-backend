"""使用 SQLite Online Backup API 创建一致性备份并输出校验清单。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    """分块计算备份文件摘要，避免一次性把数据库读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity_check(path: Path) -> str:
    """执行 SQLite 完整性检查，返回标准化检查结果。"""

    with sqlite3.connect(path) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "missing integrity result")


def create_backup(source: Path, output_dir: Path) -> tuple[Path, Path]:
    """在线备份 SQLite；即使服务正在读写，也能获得一致性快照。"""

    source = source.expanduser().resolve(strict=True)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    source_integrity = _integrity_check(source)
    if source_integrity.lower() != "ok":
        raise RuntimeError(f"源数据库完整性检查失败: {source_integrity}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output_dir / f"{source.stem}-{timestamp}{source.suffix}"
    sequence = 1
    while target.exists():
        target = output_dir / f"{source.stem}-{timestamp}-{sequence}{source.suffix}"
        sequence += 1

    # mode=ro 防止备份程序意外修改源数据库；backup() 会正确处理 WAL 快照。
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)

    target_integrity = _integrity_check(target)
    if target_integrity.lower() != "ok":
        raise RuntimeError(f"备份数据库完整性检查失败: {target_integrity}")

    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "backup": str(target),
        "source_integrity": source_integrity,
        "backup_integrity": target_integrity,
        "size_bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }
    manifest_path = target.with_suffix(f"{target.suffix}.manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target, manifest_path


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

    backup, manifest = create_backup(args.source, args.output_dir)
    print(
        json.dumps(
            {"backup": str(backup), "manifest": str(manifest), "status": "ok"},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
