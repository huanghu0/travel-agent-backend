import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from app.persistence.database import (
    MySQLDatabaseConfig,
    check_mysql_health,
    create_mysql_engine,
)
from app.persistence.schema_validation import validate_mysql_schema
from app.persistence.sqlalchemy_models import Base
from scripts.init_mysql_databases import _quoted_database


EXPECTED_TABLES = {
    "agent_sessions",
    "route_cache",
    "restaurant_cache",
    "trip_plan_versions",
    "trip_drafts",
    "trip_planning_tasks",
    "trip_task_events",
    "data_migration_batches",
    "data_migration_records",
}


class MySQLDatabaseConfigTests(unittest.TestCase):
    def test_url_escapes_special_characters_and_default_string_hides_password(self):
        config = MySQLDatabaseConfig(
            user="trip-user",
            password="p@ss:/?#[]",
            database="travel_agent",
        )

        url = config.sqlalchemy_url()

        self.assertEqual(url.password, "p@ss:/?#[]")
        self.assertNotIn("p@ss:/?#[]", str(url))
        self.assertIn("***", str(url))
        self.assertIn("p%40ss%3A%2F%3F%23%5B%5D", url.render_as_string(hide_password=False))

    def test_safe_target_excludes_user_and_password(self):
        config = MySQLDatabaseConfig(
            host="mysql.local",
            port=3307,
            database="travel_agent",
            user="secret-user",
            password="secret-password",
        )

        target = config.safe_target()

        self.assertEqual(target, "mysql.local:3307/travel_agent")
        self.assertNotIn(config.user, target)
        self.assertNotIn(config.password, target)

    def test_from_settings_supports_database_override(self):
        settings = SimpleNamespace(
            MYSQL_HOST="127.0.0.1",
            MYSQL_PORT=3306,
            MYSQL_DATABASE="travel_agent",
            MYSQL_USER="app",
            MYSQL_PASSWORD="secret",
            MYSQL_CHARSET="utf8mb4",
            MYSQL_POOL_SIZE=5,
            MYSQL_MAX_OVERFLOW=7,
            MYSQL_POOL_RECYCLE_SECONDS=1200,
            MYSQL_POOL_PRE_PING=True,
            MYSQL_CONNECT_TIMEOUT_SECONDS=3,
            MYSQL_READ_TIMEOUT_SECONDS=20,
            MYSQL_WRITE_TIMEOUT_SECONDS=25,
        )

        config = MySQLDatabaseConfig.from_settings(
            settings,
            database="travel_agent_test",
        )

        self.assertEqual(config.database, "travel_agent_test")
        self.assertEqual(config.pool_size, 5)
        self.assertEqual(config.max_overflow, 7)
        self.assertEqual(config.connect_timeout_seconds, 3)

    def test_engine_passes_pool_and_timeout_options_to_sqlalchemy(self):
        config = MySQLDatabaseConfig(
            pool_size=3,
            max_overflow=4,
            pool_recycle_seconds=900,
            pool_pre_ping=True,
            connect_timeout_seconds=2,
            read_timeout_seconds=11,
            write_timeout_seconds=12,
        )
        sentinel_engine = object()

        with patch(
            "app.persistence.database.create_engine",
            return_value=sentinel_engine,
        ) as create_engine_mock:
            result = create_mysql_engine(config)

        self.assertIs(result, sentinel_engine)
        args, kwargs = create_engine_mock.call_args
        self.assertEqual(args[0].drivername, "mysql+pymysql")
        self.assertEqual(args[0].query["charset"], "utf8mb4")
        self.assertEqual(kwargs["pool_size"], 3)
        self.assertEqual(kwargs["max_overflow"], 4)
        self.assertEqual(kwargs["pool_recycle"], 900)
        self.assertTrue(kwargs["pool_pre_ping"])
        self.assertEqual(kwargs["isolation_level"], "READ COMMITTED")
        self.assertEqual(
            kwargs["connect_args"],
            {
                "connect_timeout": 2,
                "read_timeout": 11,
                "write_timeout": 12,
            },
        )

    def test_health_check_redacts_raw_and_encoded_password(self):
        password = "p@ss/word"
        config = MySQLDatabaseConfig(password=password)

        class FailingEngine:
            def connect(self):
                raise RuntimeError(
                    "connection failed for p@ss/word and p%40ss%2Fword"
                )

        health = check_mysql_health(FailingEngine(), config)

        self.assertFalse(health.healthy)
        self.assertNotIn(password, health.error or "")
        self.assertNotIn("p%40ss%2Fword", health.error or "")
        self.assertIn("***", health.error or "")


class MySQLMetadataTests(unittest.TestCase):
    def test_metadata_contains_business_and_migration_audit_tables(self):
        self.assertEqual(set(Base.metadata.tables), EXPECTED_TABLES)

    def test_mysql_ddl_uses_longtext_datetime6_innodb_and_utf8mb4(self):
        statements = {
            table_name: str(
                CreateTable(table).compile(dialect=mysql.dialect())
            ).upper()
            for table_name, table in Base.metadata.tables.items()
        }
        combined = "\n".join(statements.values())

        self.assertIn("LONGTEXT", combined)
        self.assertIn("DATETIME(6)", combined)
        self.assertIn("ENGINE=INNODB", combined)
        self.assertIn("CHARSET=UTF8MB4", combined)
        self.assertIn("COLLATE UTF8MB4_UNICODE_CI", combined)
        self.assertIn("BIGINT UNSIGNED", statements["trip_task_events"])

    def test_longtext_column_has_no_server_default(self):
        issue_codes = Base.metadata.tables["agent_sessions"].c.issue_codes_json

        self.assertIsNone(issue_codes.server_default)

    def test_task_event_foreign_key_targets_task_table(self):
        event_table = Base.metadata.tables["trip_task_events"]
        foreign_keys = list(event_table.foreign_keys)

        self.assertEqual(len(foreign_keys), 1)
        self.assertEqual(
            foreign_keys[0].target_fullname,
            "trip_planning_tasks.task_id",
        )

    def test_schema_validator_rejects_non_mysql_engine(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            result = validate_mysql_schema(engine)
        finally:
            engine.dispose()

        self.assertFalse(result.valid)
        self.assertIn("需要 MySQL 方言", result.errors[0])


class MySQLInitializationInputTests(unittest.TestCase):
    def test_database_name_is_quoted_after_validation(self):
        self.assertEqual(_quoted_database("travel_agent_test"), "`travel_agent_test`")

    def test_database_name_rejects_sql_fragments(self):
        with self.assertRaises(ValueError):
            _quoted_database("travel_agent; DROP DATABASE mysql")


@unittest.skipUnless(
    os.getenv("RUN_MYSQL_INTEGRATION_TESTS") == "1",
    "设置 RUN_MYSQL_INTEGRATION_TESTS=1 后执行本地 MySQL 集成校验",
)
class MySQLLiveSchemaTests(unittest.TestCase):
    def test_local_mysql_schema_matches_metadata(self):
        from app.core.config import settings

        config = MySQLDatabaseConfig.from_settings(settings)
        engine = create_mysql_engine(config)
        try:
            result = validate_mysql_schema(engine)
        finally:
            engine.dispose()

        self.assertTrue(result.valid, result.errors)
        self.assertEqual(set(result.tables), EXPECTED_TABLES)


if __name__ == "__main__":
    unittest.main()
