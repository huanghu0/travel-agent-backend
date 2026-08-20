"""使用 SQLite Online Backup API 创建一致性快照。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def sha256_path(path: Path) -> str:
    """分块计算文件摘要，避免一次性把数据库读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_integrity_check(path: Path) -> str:
    """执行 SQLite 完整性检查，返回标准化检查结果。"""

    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "missing integrity result")


def create_sqlite_backup(source: Path, output_dir: Path) -> tuple[Path, Path]:
    """在线备份 SQLite；服务仍在读写时也能得到一致性快照。"""

    source = source.expanduser().resolve(strict=True)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    source_integrity = sqlite_integrity_check(source)
    if source_integrity.lower() != "ok":
        raise RuntimeError(f"源数据库完整性检查失败: {source_integrity}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = output_dir / f"{source.stem}-{timestamp}{source.suffix}"
    sequence = 1
    while target.exists():
        target = output_dir / f"{source.stem}-{timestamp}-{sequence}{source.suffix}"
        sequence += 1

    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(target)) as target_connection:
            source_connection.backup(target_connection)

    target_integrity = sqlite_integrity_check(target)
    if target_integrity.lower() != "ok":
        raise RuntimeError(f"备份数据库完整性检查失败: {target_integrity}")

    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "backup": str(target),
        "source_integrity": source_integrity,
        "backup_integrity": target_integrity,
        "size_bytes": target.stat().st_size,
        "sha256": sha256_path(target),
    }
    manifest_path = target.with_suffix(f"{target.suffix}.manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target, manifest_path
