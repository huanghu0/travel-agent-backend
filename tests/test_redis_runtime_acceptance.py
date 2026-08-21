import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import settings
from app.evaluation.redis_runtime_acceptance import (
    RedisRuntimeAcceptanceCase,
    RedisRuntimeAcceptanceReport,
    _cleanup_acceptance_tasks,
    _find_redis_server,
    _run_case,
    run_redis_runtime_acceptance,
    write_redis_runtime_report,
)


class _RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)


class _BeginContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, traceback):
        return False


class _RecordingEngine:
    def __init__(self):
        self.connection = _RecordingConnection()

    def begin(self):
        return _BeginContext(self.connection)


class RedisRuntimeAcceptanceReportTests(unittest.TestCase):
    def test_report_contains_summary_counts(self):
        report = RedisRuntimeAcceptanceReport(
            started_at="2026-08-21T10:00:00+0800",
            database="travel_agent_test",
            redis_target="redis://127.0.0.1:6379/0",
            cases=(
                RedisRuntimeAcceptanceCase("passed", True, 1.0, {}),
                RedisRuntimeAcceptanceCase("failed", False, 2.0, {}, "boom"),
            ),
        )

        payload = report.model_dump()

        self.assertFalse(report.passed)
        self.assertFalse(payload["passed"])
        self.assertEqual(1, payload["passed_cases"])
        self.assertEqual(2, payload["total_cases"])

    def test_report_is_written_as_utf8_json(self):
        report = RedisRuntimeAcceptanceReport(
            started_at="2026-08-21T10:00:00+0800",
            database="travel_agent_test",
            redis_target="redis://127.0.0.1:6379/0",
            cases=(RedisRuntimeAcceptanceCase("中文场景", True, 1.0, {}),),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            output = write_redis_runtime_report(
                report,
                Path(tempdir) / "reports" / "redis.json",
            )

            content = output.read_text(encoding="utf-8")

        self.assertIn("中文场景", content)
        self.assertIn('"passed": true', content)


class RedisRuntimeAcceptanceSafetyTests(unittest.TestCase):
    def test_production_database_is_rejected_before_connecting(self):
        with self.assertRaisesRegex(ValueError, "拒绝使用 MYSQL_DATABASE"):
            run_redis_runtime_acceptance(mysql_database=settings.MYSQL_DATABASE)

    def test_cleanup_only_uses_requested_acceptance_prefix(self):
        engine = _RecordingEngine()

        _cleanup_acceptance_tasks(engine, "redis-acceptance-safe-")

        self.assertEqual(2, len(engine.connection.statements))
        all_parameters = []
        for statement in engine.connection.statements:
            all_parameters.extend(statement.compile().params.values())
        self.assertIn("redis-acceptance-safe-%", all_parameters)
        self.assertNotIn("%", all_parameters)

    def test_missing_redis_server_returns_structured_precondition_error(self):
        with patch(
            "app.evaluation.redis_runtime_acceptance.Path.is_file",
            return_value=False,
        ), patch(
            "app.evaluation.redis_runtime_acceptance.shutil.which",
            return_value=None,
        ):
            with self.assertRaisesRegex(FileNotFoundError, "未找到 redis-server"):
                _find_redis_server("Z:/missing/redis-server.exe")

    def test_case_exception_becomes_failed_result(self):
        def fail():
            raise RuntimeError("injected failure")

        result = _run_case("fault", fail)

        self.assertFalse(result.passed)
        self.assertEqual({}, result.details)
        self.assertIn("RuntimeError: injected failure", result.error)

    def test_one_failed_case_does_not_prevent_later_case_execution(self):
        calls = []

        def fail():
            calls.append("fail")
            raise RuntimeError("boom")

        def succeed():
            calls.append("succeed")
            return {"passed": True, "value": 1}

        results = [_run_case("first", fail), _run_case("second", succeed)]

        self.assertEqual(["fail", "succeed"], calls)
        self.assertFalse(results[0].passed)
        self.assertTrue(results[1].passed)
        self.assertEqual(1, results[1].details["value"])


if __name__ == "__main__":
    unittest.main()
