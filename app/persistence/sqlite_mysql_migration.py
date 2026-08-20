"""SQLite 历史数据迁移到 MySQL 的确定性执行、校验与安全回滚。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

from sqlalchemy import Engine, Table, delete, inspect, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.persistence.sqlalchemy_models import Base


UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class TableMigrationSpec:
    """描述一张 SQLite 表如何无损映射到同名 MySQL 表。"""

    name: str
    primary_key: tuple[str, ...]
    columns: tuple[str, ...]
    datetime_columns: frozenset[str] = frozenset()
    boolean_columns: frozenset[str] = frozenset()
    json_columns: frozenset[str] = frozenset()
    defaults: Mapping[str, Any] = field(default_factory=dict)

    @property
    def target_table(self) -> Table:
        return Base.metadata.tables[self.name]


TABLE_SPECS: tuple[TableMigrationSpec, ...] = (
    TableMigrationSpec(
        name="agent_sessions",
        primary_key=("session_id",),
        columns=(
            "session_id", "status", "city", "current_step", "max_steps",
            "action_count", "state_json", "created_at", "updated_at",
            "travel_days", "transportation", "completion_mode", "quality_level",
            "quality_score", "warning_count", "issue_codes_json",
            "tool_call_count", "llm_call_count", "total_duration_ms",
        ),
        datetime_columns=frozenset({"created_at", "updated_at"}),
        json_columns=frozenset({"state_json", "issue_codes_json"}),
        defaults={
            "travel_days": None, "transportation": None, "completion_mode": None,
            "quality_level": None, "quality_score": None, "warning_count": 0,
            "issue_codes_json": "[]", "tool_call_count": 0,
            "llm_call_count": 0, "total_duration_ms": 0,
        },
    ),
    TableMigrationSpec(
        name="route_cache",
        primary_key=("cache_key",),
        columns=("cache_key", "provider", "mode", "estimate_json", "created_at", "expires_at"),
        datetime_columns=frozenset({"created_at", "expires_at"}),
        json_columns=frozenset({"estimate_json"}),
    ),
    TableMigrationSpec(
        name="restaurant_cache",
        primary_key=("cache_key",),
        columns=(
            "cache_key", "provider", "city", "keywords", "snapshot_json",
            "created_at", "expires_at",
        ),
        datetime_columns=frozenset({"created_at", "expires_at"}),
        json_columns=frozenset({"snapshot_json"}),
    ),
    TableMigrationSpec(
        name="trip_plan_versions",
        primary_key=("version_id",),
        columns=(
            "version_id", "session_id", "version_number", "status", "source",
            "source_draft_id", "version_json", "created_at", "confirmed_at",
        ),
        datetime_columns=frozenset({"created_at", "confirmed_at"}),
        json_columns=frozenset({"version_json"}),
        defaults={"source_draft_id": None, "confirmed_at": None},
    ),
    TableMigrationSpec(
        name="trip_drafts",
        primary_key=("draft_id",),
        columns=(
            "draft_id", "session_id", "base_version", "status", "draft_json",
            "candidate_version_id", "created_at", "updated_at",
        ),
        datetime_columns=frozenset({"created_at", "updated_at"}),
        json_columns=frozenset({"draft_json"}),
        defaults={"candidate_version_id": None},
    ),
    TableMigrationSpec(
        name="trip_planning_tasks",
        primary_key=("task_id",),
        columns=(
            "task_id", "session_id", "idempotency_key", "request_fingerprint",
            "status", "cancel_requested", "worker_id", "lease_expires_at",
            "heartbeat_at", "task_json", "created_at", "updated_at",
        ),
        datetime_columns=frozenset(
            {"lease_expires_at", "heartbeat_at", "created_at", "updated_at"}
        ),
        boolean_columns=frozenset({"cancel_requested"}),
        json_columns=frozenset({"task_json"}),
        defaults={"worker_id": None, "lease_expires_at": None, "heartbeat_at": None},
    ),
    TableMigrationSpec(
        name="trip_task_events",
        primary_key=("event_id",),
        columns=("event_id", "task_id", "event_type", "event_json", "created_at"),
        datetime_columns=frozenset({"created_at"}),
        json_columns=frozenset({"event_json"}),
    ),
)

SPEC_BY_NAME = {spec.name: spec for spec in TABLE_SPECS}
BATCH_TABLE = Base.metadata.tables["data_migration_batches"]
RECORD_TABLE = Base.metadata.tables["data_migration_records"]


class SQLiteMySQLMigrationError(RuntimeError):
    """迁移输入、目标结构或批次状态不满足安全约束。"""


class MigrationConflictError(SQLiteMySQLMigrationError):
    """源主键与目标已有不同数据冲突。"""


def utc_now_naive() -> datetime:
    """MySQL DATETIME(6) 统一保存无时区 UTC。"""

    return datetime.now(UTC).replace(tzinfo=None)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_utc_datetime(value: Any) -> datetime | None:
    """把 SQLite ISO 时间或 datetime 统一成无时区 UTC。"""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise SQLiteMySQLMigrationError(f"无效时间值: {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    raise TypeError(f"不支持摘要序列化的类型: {type(value).__name__}")


def row_sha256(spec: TableMigrationSpec, row: Mapping[str, Any]) -> str:
    """对目标列生成稳定摘要，供 verify 和 rollback 使用。"""

    payload = {name: row.get(name) for name in spec.columns}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_key(spec: TableMigrationSpec, row: Mapping[str, Any]) -> str:
    values = [row[name] for name in spec.primary_key]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str)


def _target_key_json(spec: TableMigrationSpec, row: Mapping[str, Any]) -> str:
    return json.dumps(
        {name: row[name] for name in spec.primary_key},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _key_predicate(spec: TableMigrationSpec, row: Mapping[str, Any]):
    clauses = [spec.target_table.c[name] == row[name] for name in spec.primary_key]
    predicate = clauses[0]
    for clause in clauses[1:]:
        predicate = predicate & clause
    return predicate


def normalize_source_row(
    spec: TableMigrationSpec,
    raw_row: Mapping[str, Any],
) -> dict[str, Any]:
    """补齐旧 SQLite 默认列并转换时间、布尔值和 JSON。"""

    available = set(raw_row.keys())
    normalized: dict[str, Any] = {}
    for column_name in spec.columns:
        if column_name in available:
            value = raw_row[column_name]
        elif column_name in spec.defaults:
            value = spec.defaults[column_name]
        else:
            raise SQLiteMySQLMigrationError(
                f"SQLite 表 {spec.name} 缺少必需列 {column_name}"
            )
        if column_name in spec.datetime_columns:
            value = parse_utc_datetime(value)
        elif column_name in spec.boolean_columns:
            value = bool(value)
        normalized[column_name] = value

    for column_name in spec.json_columns:
        raw_json = normalized[column_name]
        if not isinstance(raw_json, str):
            raise SQLiteMySQLMigrationError(
                f"{spec.name}.{column_name} 必须是 JSON 字符串"
            )
        try:
            json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise SQLiteMySQLMigrationError(
                f"{spec.name}.{column_name} 包含无效 JSON: {exc.msg}"
            ) from exc

    # 在写入前检查 VARCHAR 长度，避免 MySQL 静默截断或执行到中途失败。
    table = spec.target_table
    for column_name, value in normalized.items():
        length = getattr(table.c[column_name].type, "length", None)
        if value is not None and length is not None and len(str(value)) > length:
            raise SQLiteMySQLMigrationError(
                f"{spec.name}.{column_name} 长度 {len(str(value))} 超过 MySQL 上限 {length}"
            )
    return normalized


def normalize_target_row(
    spec: TableMigrationSpec,
    raw_row: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = {name: raw_row[name] for name in spec.columns}
    for name in spec.datetime_columns:
        normalized[name] = parse_utc_datetime(normalized[name])
    for name in spec.boolean_columns:
        normalized[name] = bool(normalized[name])
    return normalized


@contextmanager
def open_sqlite_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    resolved = path.expanduser().resolve(strict=True)
    uri = f"file:{resolved.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise SQLiteMySQLMigrationError(
                f"SQLite 完整性检查失败: {integrity[0] if integrity else '无结果'}"
            )
        yield connection
    finally:
        connection.close()


def _existing_source_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def iter_source_rows(
    connection: sqlite3.Connection,
    spec: TableMigrationSpec,
) -> Iterator[dict[str, Any]]:
    if spec.name not in _existing_source_tables(connection):
        return
    order = ", ".join(f'"{name}"' for name in spec.primary_key)
    cursor = connection.execute(f'SELECT * FROM "{spec.name}" ORDER BY {order}')
    for raw_row in cursor:
        yield normalize_source_row(spec, raw_row)


def _ensure_target_schema(engine: Engine) -> None:
    expected = {spec.name for spec in TABLE_SPECS} | {
        "data_migration_batches",
        "data_migration_records",
    }
    actual = set(inspect(engine).get_table_names())
    missing = sorted(expected - actual)
    if missing:
        raise SQLiteMySQLMigrationError(
            "MySQL 缺少迁移所需表: " + ", ".join(missing)
            + "；请先执行 alembic upgrade head"
        )


def _empty_table_report() -> dict[str, int]:
    return {
        "source": 0,
        "inserted": 0,
        "resumed": 0,
        "existing_same": 0,
        "conflicts": 0,
        "missing": 0,
        "matched": 0,
        "invalid": 0,
    }


def _sum_reports(reports: Mapping[str, Mapping[str, int]]) -> dict[str, int]:
    keys = next(iter(reports.values())).keys() if reports else ()
    return {key: sum(report.get(key, 0) for report in reports.values()) for key in keys}


class SQLiteToMySQLMigrator:
    """以主键幂等迁移七张业务表，并保留逐行安全回滚凭证。"""

    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def _migration_lock(self, timeout_seconds: int = 5) -> Iterator[None]:
        """MySQL 使用命名锁禁止两个迁移进程同时写入。"""

        if self.engine.dialect.name != "mysql":
            yield
            return
        with self.engine.connect() as connection:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": "travel_agent_sqlite_to_mysql", "timeout": timeout_seconds},
            ).scalar_one()
            if int(acquired or 0) != 1:
                raise SQLiteMySQLMigrationError("无法取得 MySQL 历史迁移锁")
            try:
                yield
            finally:
                connection.execute(
                    text("SELECT RELEASE_LOCK(:name)"),
                    {"name": "travel_agent_sqlite_to_mysql"},
                )

    def dry_run(self, source_path: Path) -> dict[str, Any]:
        """只读验证源数据，并统计目标同值、缺失和冲突记录。"""

        source_path = source_path.expanduser().resolve(strict=True)
        _ensure_target_schema(self.engine)
        reports = {spec.name: _empty_table_report() for spec in TABLE_SPECS}
        errors: list[dict[str, str]] = []
        with open_sqlite_readonly(source_path) as source:
            for spec in TABLE_SPECS:
                try:
                    rows = iter_source_rows(source, spec)
                    with self.engine.connect() as target:
                        for row in rows:
                            reports[spec.name]["source"] += 1
                            existing = target.execute(
                                select(spec.target_table).where(_key_predicate(spec, row))
                            ).mappings().first()
                            if existing is None:
                                reports[spec.name]["missing"] += 1
                            elif row_sha256(spec, normalize_target_row(spec, existing)) == row_sha256(spec, row):
                                reports[spec.name]["existing_same"] += 1
                            else:
                                reports[spec.name]["conflicts"] += 1
                except Exception as exc:
                    reports[spec.name]["invalid"] += 1
                    errors.append({"table": spec.name, "message": str(exc)})
        totals = _sum_reports(reports)
        return {
            "action": "dry-run",
            "source_path": str(source_path),
            "source_sha256": sha256_file(source_path),
            "source_size_bytes": source_path.stat().st_size,
            "tables": reports,
            "totals": totals,
            "valid": not errors,
            "has_conflicts": totals["conflicts"] > 0,
            "errors": errors,
        }

    def execute(
        self,
        source_path: Path,
        *,
        original_source_path: Path | None = None,
        backup_path: Path | None = None,
        resume_batch_id: str | None = None,
        fail_on_conflict: bool = True,
    ) -> dict[str, Any]:
        """幂等插入缺失行；默认遇到不同值主键冲突即失败。"""

        source_path = source_path.expanduser().resolve(strict=True)
        _ensure_target_schema(self.engine)
        source_hash = sha256_file(source_path)
        source_size = source_path.stat().st_size
        batch_id = resume_batch_id or str(uuid4())
        reports = {spec.name: _empty_table_report() for spec in TABLE_SPECS}
        started_at = utc_now_naive()

        with self._migration_lock():
            if resume_batch_id:
                self._prepare_resume(batch_id, source_hash, reports)
            else:
                with self.engine.begin() as connection:
                    connection.execute(
                        insert(BATCH_TABLE).values(
                            batch_id=batch_id,
                            source_path=str((original_source_path or source_path).resolve()),
                            source_sha256=source_hash,
                            source_size_bytes=source_size,
                            backup_path=str(backup_path.resolve()) if backup_path else None,
                            status="running",
                            summary_json="{}",
                            error_text=None,
                            started_at=started_at,
                            completed_at=None,
                        )
                    )
            try:
                with open_sqlite_readonly(source_path) as source:
                    for spec in TABLE_SPECS:
                        for row in iter_source_rows(source, spec):
                            reports[spec.name]["source"] += 1
                            self._migrate_row(batch_id, spec, row, reports[spec.name])
                totals = _sum_reports(reports)
                if fail_on_conflict and totals["conflicts"]:
                    raise MigrationConflictError(
                        f"发现 {totals['conflicts']} 条目标主键冲突，未覆盖 MySQL 现有数据"
                    )
                status = "completed_with_conflicts" if totals["conflicts"] else "completed"
                result = {
                    "action": "execute",
                    "batch_id": batch_id,
                    "status": status,
                    "source_path": str(source_path),
                    "source_sha256": source_hash,
                    "backup_path": str(backup_path.resolve()) if backup_path else None,
                    "tables": reports,
                    "totals": totals,
                }
                self._finish_batch(batch_id, status, result)
                return result
            except Exception as exc:
                failed_result = {
                    "action": "execute",
                    "batch_id": batch_id,
                    "status": "failed",
                    "source_path": str(source_path),
                    "source_sha256": source_hash,
                    "backup_path": str(backup_path.resolve()) if backup_path else None,
                    "tables": reports,
                    "totals": _sum_reports(reports),
                    "error": str(exc),
                }
                self._finish_batch(batch_id, "failed", failed_result, error=str(exc))
                # CLI 通过该属性返回可恢复/可回滚的批次号，不把它埋在日志里。
                setattr(exc, "batch_id", batch_id)
                raise

    def _prepare_resume(
        self,
        batch_id: str,
        source_hash: str,
        reports: dict[str, dict[str, int]],
    ) -> None:
        with self.engine.begin() as connection:
            batch = connection.execute(
                select(BATCH_TABLE).where(BATCH_TABLE.c.batch_id == batch_id).with_for_update()
            ).mappings().first()
            if batch is None:
                raise SQLiteMySQLMigrationError(f"迁移批次不存在: {batch_id}")
            if batch["source_sha256"] != source_hash:
                raise SQLiteMySQLMigrationError("恢复迁移时源快照 SHA-256 不一致")
            if batch["status"] not in {"running", "failed"}:
                raise SQLiteMySQLMigrationError(
                    f"迁移批次状态 {batch['status']} 不允许恢复"
                )
            # 已完成行由逐行凭证在重新扫描时计入 resumed，避免累计值重复。
            connection.execute(
                update(BATCH_TABLE)
                .where(BATCH_TABLE.c.batch_id == batch_id)
                .values(status="running", error_text=None, completed_at=None)
            )

    def _migrate_row(
        self,
        batch_id: str,
        spec: TableMigrationSpec,
        row: Mapping[str, Any],
        report: dict[str, int],
    ) -> None:
        source_key = _source_key(spec, row)
        digest = row_sha256(spec, row)
        with self.engine.begin() as connection:
            recorded = connection.execute(
                select(RECORD_TABLE).where(
                    (RECORD_TABLE.c.batch_id == batch_id)
                    & (RECORD_TABLE.c.table_name == spec.name)
                    & (RECORD_TABLE.c.source_key == source_key)
                )
            ).mappings().first()
            existing = connection.execute(
                select(spec.target_table).where(_key_predicate(spec, row)).with_for_update()
            ).mappings().first()

            if recorded is not None:
                if existing is None:
                    raise SQLiteMySQLMigrationError(
                        f"批次凭证存在但目标行缺失: {spec.name} {source_key}"
                    )
                current_hash = row_sha256(spec, normalize_target_row(spec, existing))
                if current_hash != recorded["row_sha256"]:
                    raise SQLiteMySQLMigrationError(
                        f"恢复迁移时目标行已被修改: {spec.name} {source_key}"
                    )
                report["resumed"] += 1
                return

            if existing is not None:
                current_hash = row_sha256(spec, normalize_target_row(spec, existing))
                if current_hash == digest:
                    report["existing_same"] += 1
                else:
                    report["conflicts"] += 1
                return

            try:
                connection.execute(insert(spec.target_table).values(**row))
                connection.execute(
                    insert(RECORD_TABLE).values(
                        batch_id=batch_id,
                        table_name=spec.name,
                        source_key=source_key,
                        target_key_json=_target_key_json(spec, row),
                        row_sha256=digest,
                        created_at=utc_now_naive(),
                    )
                )
                report["inserted"] += 1
            except IntegrityError as exc:
                raise MigrationConflictError(
                    f"写入 {spec.name} {source_key} 时发生唯一键或外键冲突"
                ) from exc

    def _finish_batch(
        self,
        batch_id: str,
        status: str,
        result: Mapping[str, Any],
        *,
        error: str | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(BATCH_TABLE)
                .where(BATCH_TABLE.c.batch_id == batch_id)
                .values(
                    status=status,
                    summary_json=json.dumps(result, ensure_ascii=False, default=_json_default),
                    error_text=error,
                    completed_at=utc_now_naive(),
                )
            )

    def verify(self, source_path: Path, *, batch_id: str | None = None) -> dict[str, Any]:
        """逐行比较源快照和 MySQL，只有全部匹配才允许切换后端。"""

        source_path = source_path.expanduser().resolve(strict=True)
        _ensure_target_schema(self.engine)
        source_hash = sha256_file(source_path)
        if batch_id:
            with self.engine.connect() as connection:
                batch = connection.execute(
                    select(BATCH_TABLE).where(BATCH_TABLE.c.batch_id == batch_id)
                ).mappings().first()
            if batch is None:
                raise SQLiteMySQLMigrationError(f"迁移批次不存在: {batch_id}")
            if batch["source_sha256"] != source_hash:
                raise SQLiteMySQLMigrationError("验证源快照与迁移批次 SHA-256 不一致")

        reports = {spec.name: _empty_table_report() for spec in TABLE_SPECS}
        with open_sqlite_readonly(source_path) as source:
            for spec in TABLE_SPECS:
                with self.engine.connect() as target:
                    for row in iter_source_rows(source, spec):
                        reports[spec.name]["source"] += 1
                        existing = target.execute(
                            select(spec.target_table).where(_key_predicate(spec, row))
                        ).mappings().first()
                        if existing is None:
                            reports[spec.name]["missing"] += 1
                        elif row_sha256(spec, normalize_target_row(spec, existing)) == row_sha256(spec, row):
                            reports[spec.name]["matched"] += 1
                        else:
                            reports[spec.name]["conflicts"] += 1
        totals = _sum_reports(reports)
        verified = totals["source"] == totals["matched"] and not totals["missing"] and not totals["conflicts"]
        result = {
            "action": "verify",
            "batch_id": batch_id,
            "source_path": str(source_path),
            "source_sha256": source_hash,
            "tables": reports,
            "totals": totals,
            "verified": verified,
            "safe_to_cutover": verified,
        }
        if batch_id:
            self._finish_batch(
                batch_id,
                "verified" if verified else "verification_failed",
                result,
                error=None if verified else "源快照与 MySQL 数据不完全一致",
            )
        return result

    def rollback(self, batch_id: str) -> dict[str, Any]:
        """仅删除该批次插入且此后未被修改的数据。"""

        _ensure_target_schema(self.engine)
        reports = {spec.name: {"tracked": 0, "deleted": 0, "missing": 0, "protected_modified": 0} for spec in TABLE_SPECS}
        with self._migration_lock():
            with self.engine.connect() as connection:
                batch = connection.execute(
                    select(BATCH_TABLE).where(BATCH_TABLE.c.batch_id == batch_id)
                ).mappings().first()
            if batch is None:
                raise SQLiteMySQLMigrationError(f"迁移批次不存在: {batch_id}")
            if batch["status"] in {"rolled_back", "rollback_partial"}:
                raise SQLiteMySQLMigrationError(f"迁移批次已经回滚: {batch_id}")

            for spec in reversed(TABLE_SPECS):
                with self.engine.connect() as connection:
                    records = connection.execute(
                        select(RECORD_TABLE)
                        .where(
                            (RECORD_TABLE.c.batch_id == batch_id)
                            & (RECORD_TABLE.c.table_name == spec.name)
                        )
                        .order_by(RECORD_TABLE.c.source_key)
                    ).mappings().all()
                for record in records:
                    reports[spec.name]["tracked"] += 1
                    key_values = json.loads(record["target_key_json"])
                    self._rollback_row(spec, key_values, record["row_sha256"], reports[spec.name])

            totals = {
                key: sum(report[key] for report in reports.values())
                for key in ("tracked", "deleted", "missing", "protected_modified")
            }
            partial = totals["protected_modified"] > 0
            status = "rollback_partial" if partial else "rolled_back"
            result = {
                "action": "rollback",
                "batch_id": batch_id,
                "status": status,
                "tables": reports,
                "totals": totals,
                "fully_rolled_back": not partial,
            }
            self._finish_batch(
                batch_id,
                status,
                result,
                error="部分目标行已被后续修改，已保护不删除" if partial else None,
            )
            return result

    def _rollback_row(
        self,
        spec: TableMigrationSpec,
        key_values: Mapping[str, Any],
        expected_hash: str,
        report: dict[str, int],
    ) -> None:
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(spec.target_table)
                .where(_key_predicate(spec, key_values))
                .with_for_update()
            ).mappings().first()
            if existing is None:
                report["missing"] += 1
                return
            current_hash = row_sha256(spec, normalize_target_row(spec, existing))
            if current_hash != expected_hash:
                report["protected_modified"] += 1
                return
            connection.execute(
                delete(spec.target_table).where(_key_predicate(spec, key_values))
            )
            report["deleted"] += 1
