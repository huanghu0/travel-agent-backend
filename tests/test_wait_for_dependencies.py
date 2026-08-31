from __future__ import annotations

import io
import math
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts import wait_for_dependencies as readiness


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class DependencyReadinessTests(unittest.TestCase):
    def test_recomputes_deadline_before_each_serial_probe(self) -> None:
        clock = FakeClock()
        calls = []

        def first_probe(timeout: float) -> bool:
            calls.append(("mysql", timeout))
            clock.now += timeout
            return False

        def second_probe(timeout: float) -> bool:
            calls.append(("qdrant", timeout))
            clock.now += timeout
            return False

        errors = io.StringIO()
        ready = readiness.wait_for_dependencies(
            probes={"mysql": first_probe, "qdrant": second_probe},
            timeout_seconds=1.5,
            poll_interval_seconds=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            output=io.StringIO(),
            error_output=errors,
        )

        self.assertFalse(ready)
        self.assertEqual([("mysql", 1.5)], calls)
        self.assertEqual(1.5, clock.now)
        self.assertIn(
            "dependency_wait status=timeout pending=mysql,qdrant",
            errors.getvalue(),
        )

    def test_wait_rejects_non_finite_and_non_positive_intervals(self) -> None:
        invalid_values = (math.nan, math.inf, -math.inf, 0, -1)

        for value in invalid_values:
            with self.subTest(timeout_seconds=value):
                with self.assertRaisesRegex(ValueError, "positive"):
                    readiness.wait_for_dependencies(
                        probes={},
                        timeout_seconds=value,
                    )
            with self.subTest(poll_interval_seconds=value):
                with self.assertRaisesRegex(ValueError, "positive"):
                    readiness.wait_for_dependencies(
                        probes={},
                        poll_interval_seconds=value,
                    )

    def test_wait_timeout_rejects_non_finite_values(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"DEPENDENCY_WAIT_TIMEOUT_SECONDS": str(value)},
                ):
                    with self.assertRaisesRegex(ValueError, "positive"):
                        readiness._wait_timeout()

    def test_main_returns_two_for_non_finite_wait_timeout(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                errors = io.StringIO()
                with (
                    patch.dict(
                        os.environ,
                        {"DEPENDENCY_WAIT_TIMEOUT_SECONDS": value},
                    ),
                    patch.object(
                        readiness,
                        "wait_for_dependencies",
                    ) as wait_for_dependencies,
                    redirect_stderr(errors),
                ):
                    exit_code = readiness.main()

                self.assertEqual(2, exit_code)
                wait_for_dependencies.assert_not_called()
                self.assertIn(
                    "dependency_wait status=failed error_class=ValueError",
                    errors.getvalue(),
                )

    def test_waits_until_mysql_and_qdrant_are_both_ready(self) -> None:
        clock = FakeClock()
        mysql_states = iter((False, True))
        output = io.StringIO()
        errors = io.StringIO()

        ready = readiness.wait_for_dependencies(
            probes={
                "mysql": lambda _timeout: next(mysql_states),
                "qdrant": lambda _timeout: True,
            },
            timeout_seconds=3,
            poll_interval_seconds=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            output=output,
            error_output=errors,
        )

        self.assertTrue(ready)
        self.assertEqual(
            [
                "dependency=qdrant status=ready",
                "dependency=mysql status=ready",
            ],
            output.getvalue().splitlines(),
        )
        self.assertEqual("", errors.getvalue())

    def test_timeout_reports_only_dependency_names(self) -> None:
        clock = FakeClock()
        output = io.StringIO()
        errors = io.StringIO()
        private_error = "https://user:password@qdrant/private sk-private-value"

        def unavailable(_timeout: float) -> bool:
            raise RuntimeError(private_error)

        ready = readiness.wait_for_dependencies(
            probes={"mysql": unavailable, "qdrant": unavailable},
            timeout_seconds=2,
            poll_interval_seconds=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            output=output,
            error_output=errors,
        )

        self.assertFalse(ready)
        self.assertEqual("", output.getvalue())
        self.assertIn(
            "dependency_wait status=timeout pending=mysql,qdrant",
            errors.getvalue(),
        )
        self.assertNotIn(private_error, errors.getvalue())
        self.assertNotIn("password", errors.getvalue())

    def test_qdrant_probe_uses_readyz_and_optional_api_key(self) -> None:
        with patch(
            "scripts.wait_for_dependencies.urlopen",
            return_value=Response(),
        ) as urlopen:
            ready = readiness.probe_qdrant(
                "http://qdrant:6333/",
                "qdrant-test-key",
                1.5,
            )

        self.assertTrue(ready)
        request = urlopen.call_args.args[0]
        self.assertEqual("http://qdrant:6333/readyz", request.full_url)
        self.assertEqual("qdrant-test-key", request.get_header("Api-key"))
        self.assertEqual(1.5, urlopen.call_args.kwargs["timeout"])

    def test_wait_is_capped_at_sixty_seconds(self) -> None:
        clock = FakeClock()

        ready = readiness.wait_for_dependencies(
            probes={"mysql": lambda _timeout: False},
            timeout_seconds=600,
            poll_interval_seconds=10,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            output=io.StringIO(),
            error_output=io.StringIO(),
        )

        self.assertFalse(ready)
        self.assertEqual(60.0, clock.now)


if __name__ == "__main__":
    unittest.main()
