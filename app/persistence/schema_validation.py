"""MySQL 物理 Schema 验证，确保 Alembic 结果与 SQLAlchemy 元数据一致。"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Engine, UniqueConstraint, inspect, text
from sqlalchemy.dialects.mysql import BIGINT, DATETIME, LONGTEXT

from app.persistence.sqlalchemy_models import Base


@dataclass(slots=True)
class MySQLSchemaValidation:
    """Schema 验证结果，可直接序列化给命令行或健康检查。"""

    valid: bool
    database: str | None
    tables: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _column_names(items: list[dict]) -> dict[str, tuple[str, ...]]:
    """把 Inspector 返回结果压缩为名称到字段元组的映射。"""

    return {
        str(item["name"]): tuple(str(name) for name in item.get("column_names") or [])
        for item in items
        if item.get("name")
    }


def validate_mysql_schema(engine: Engine) -> MySQLSchemaValidation:
    """验证业务表、迁移审计表、索引、约束和 MySQL 专用存储类型。"""

    if engine.dialect.name != "mysql":
        return MySQLSchemaValidation(
            valid=False,
            database=None,
            errors=[f"需要 MySQL 方言，实际为 {engine.dialect.name}"],
        )

    inspector = inspect(engine)
    expected_tables = set(Base.metadata.tables)
    actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
    errors: list[str] = []

    missing_tables = sorted(expected_tables - actual_tables)
    extra_tables = sorted(actual_tables - expected_tables)
    if missing_tables:
        errors.append(f"缺少业务表: {', '.join(missing_tables)}")
    if extra_tables:
        errors.append(f"存在未声明业务表: {', '.join(extra_tables)}")

    for table_name in sorted(expected_tables & actual_tables):
        model_table = Base.metadata.tables[table_name]
        reflected_columns = {
            str(column["name"]): column for column in inspector.get_columns(table_name)
        }
        expected_columns = set(model_table.columns.keys())
        actual_columns = set(reflected_columns)
        if expected_columns != actual_columns:
            missing = sorted(expected_columns - actual_columns)
            extra = sorted(actual_columns - expected_columns)
            if missing:
                errors.append(f"{table_name} 缺少字段: {', '.join(missing)}")
            if extra:
                errors.append(f"{table_name} 存在多余字段: {', '.join(extra)}")

        expected_pk = tuple(column.name for column in model_table.primary_key.columns)
        actual_pk = tuple(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or []
        )
        if expected_pk != actual_pk:
            errors.append(
                f"{table_name} 主键不一致: expected={expected_pk}, actual={actual_pk}"
            )

        expected_indexes = {
            index.name: tuple(column.name for column in index.columns)
            for index in model_table.indexes
            if index.name
        }
        actual_indexes = _column_names(inspector.get_indexes(table_name))
        for index_name, columns in expected_indexes.items():
            if actual_indexes.get(index_name) != columns:
                errors.append(
                    f"{table_name} 索引 {index_name} 不一致: "
                    f"expected={columns}, actual={actual_indexes.get(index_name)}"
                )

        expected_uniques = {
            constraint.name: tuple(column.name for column in constraint.columns)
            for constraint in model_table.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name
        }
        actual_uniques = _column_names(inspector.get_unique_constraints(table_name))
        for constraint_name, columns in expected_uniques.items():
            if actual_uniques.get(constraint_name) != columns:
                errors.append(
                    f"{table_name} 唯一约束 {constraint_name} 不一致: "
                    f"expected={columns}, actual={actual_uniques.get(constraint_name)}"
                )

        expected_foreign_keys = {
            (
                foreign_key.parent.name,
                foreign_key.column.table.name,
                foreign_key.column.name,
            )
            for foreign_key in model_table.foreign_keys
        }
        actual_foreign_keys = {
            (
                str(foreign_key["constrained_columns"][0]),
                str(foreign_key["referred_table"]),
                str(foreign_key["referred_columns"][0]),
            )
            for foreign_key in inspector.get_foreign_keys(table_name)
        }
        if expected_foreign_keys != actual_foreign_keys:
            errors.append(
                f"{table_name} 外键不一致: "
                f"expected={sorted(expected_foreign_keys)}, "
                f"actual={sorted(actual_foreign_keys)}"
            )

        for column_name in expected_columns & actual_columns:
            model_column = model_table.columns[column_name]
            reflected_column = reflected_columns[column_name]
            reflected_type = reflected_column["type"]
            compiled_type = model_column.type.dialect_impl(engine.dialect)

            if isinstance(compiled_type, LONGTEXT):
                if not isinstance(reflected_type, LONGTEXT):
                    errors.append(f"{table_name}.{column_name} 必须为 LONGTEXT")
                if reflected_column.get("default") is not None:
                    errors.append(f"{table_name}.{column_name} LONGTEXT 不应设置默认值")

            if isinstance(compiled_type, DATETIME):
                if not isinstance(reflected_type, DATETIME) or reflected_type.fsp != 6:
                    errors.append(f"{table_name}.{column_name} 必须为 DATETIME(6)")

            if isinstance(compiled_type, BIGINT) and compiled_type.unsigned:
                if not isinstance(reflected_type, BIGINT) or not reflected_type.unsigned:
                    errors.append(f"{table_name}.{column_name} 必须为 BIGINT UNSIGNED")

    database = None
    if actual_tables:
        with engine.connect() as connection:
            database = connection.execute(text("SELECT DATABASE()")).scalar_one_or_none()
            table_options = connection.execute(
                text(
                    "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    "AND TABLE_NAME <> 'alembic_version'"
                )
            ).mappings()
            for row in table_options:
                table_name = str(row["TABLE_NAME"])
                if str(row["ENGINE"]).lower() != "innodb":
                    errors.append(f"{table_name} 必须使用 InnoDB")
                if not str(row["TABLE_COLLATION"]).lower().startswith("utf8mb4_"):
                    errors.append(f"{table_name} 必须使用 utf8mb4 排序规则")

    return MySQLSchemaValidation(
        valid=not errors,
        database=database,
        tables=sorted(actual_tables),
        errors=errors,
    )
