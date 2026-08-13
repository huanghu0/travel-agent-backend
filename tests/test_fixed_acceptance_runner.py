import importlib.util
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_fixed_acceptance_baseline.py"
SPEC = importlib.util.spec_from_file_location("run_fixed_acceptance_baseline", SCRIPT_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class FixedAcceptanceRunnerTests(unittest.TestCase):
    def test_http_error_preserves_json_detail_and_status_code(self):
        body = json.dumps(
            {"detail": "旅行规划失败（阶段：路线评估）: provider unavailable"},
            ensure_ascii=False,
        ).encode("utf-8")
        error = HTTPError(
            "http://127.0.0.1:8000/api/trip/plan",
            503,
            "Service Unavailable",
            {},
            BytesIO(body),
        )

        with patch.object(runner, "urlopen", side_effect=error):
            with self.assertRaises(runner.HttpJsonError) as raised:
                runner._http_json(
                    "http://127.0.0.1:8000/api/trip/plan",
                    method="POST",
                    payload={"city": "杭州"},
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("provider unavailable", raised.exception.detail)
        self.assertEqual(raised.exception.response_body["detail"], raised.exception.detail)

    def test_structured_failure_redacts_sensitive_response_fields(self):
        error = runner.HttpJsonError(
            endpoint="http://127.0.0.1:8000/api/trip/plan",
            status_code=500,
            detail={"api_key": "plain-secret", "message": "failed"},
            response_body={"authorization": "Bearer secret-token-value"},
        )

        failure = runner._failure_from_exception(
            "hangzhou-1d-walking",
            "trip_plan",
            error,
        )

        rendered = json.dumps(runner.asdict(failure), ensure_ascii=False)
        self.assertNotIn("plain-secret", rendered)
        self.assertNotIn("secret-token-value", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertEqual(failure.status_code, 500)
        self.assertEqual(failure.stage, "trip_plan")


if __name__ == "__main__":
    unittest.main()
