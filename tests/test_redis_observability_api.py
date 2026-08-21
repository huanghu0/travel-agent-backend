"""Redis 可观测 HTTP 契约测试。"""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class _FakeObservability:
    def __init__(self) -> None:
        self.config = SimpleNamespace(prometheus_enabled=True)

    def prometheus_payload(self):
        return b"travel_agent_redis_up 1\n", "text/plain; version=0.0.4"

    def snapshot(self):
        return {
            "health": {"healthy": True, "degraded": False},
            "pool": {"max_connections": 32, "utilization": 0.25},
            "alerts": [],
            "tuning": {
                "worker_fallback_poll_seconds": 5.0,
                "sse_fallback_poll_seconds": 5.0,
            },
            "degraded": False,
        }


class RedisObservabilityApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_metrics_endpoint_returns_prometheus_payload(self):
        with patch.object(main, "redis_observability", _FakeObservability()):
            response = self.client.get("/metrics")

        self.assertEqual(200, response.status_code)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("travel_agent_redis_up 1", response.text)
        self.assertNotIn("task_id", response.text)

    def test_observability_endpoint_returns_pool_alerts_and_tuning(self):
        with patch.object(main, "redis_observability", _FakeObservability()):
            response = self.client.get("/api/observability/redis")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(32, payload["pool"]["max_connections"])
        self.assertEqual([], payload["alerts"])
        self.assertEqual(5.0, payload["tuning"]["sse_fallback_poll_seconds"])


if __name__ == "__main__":
    unittest.main()
