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
            "client_metrics": {},
            "pool": {"max_connections": 32, "utilization": 0.25},
            "notification_health": {"degraded": False},
            "cache_metrics": {},
            "layered_cache_metrics": {},
            "amap_business_cache_metrics": {},
            "provider_quota_metrics": {},
            "alerts": [],
            "tuning": {
                "worker_fallback_poll_seconds": 5.0,
                "sse_fallback_poll_seconds": 5.0,
            },
            "degraded": False,
        }


class _FakeRagRuntime:
    def health_snapshot(self, *, probe=False):
        return {
            "qdrant": "ready",
            "rag": "ready",
            "embedding_configured": True,
            "status": "ready",
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

    def test_health_retains_redis_fields_and_adds_rag_components(self):
        with (
            patch.object(main, "redis_observability", _FakeObservability()),
            patch.object(main, "rag_runtime", _FakeRagRuntime(), create=True),
        ):
            response = self.client.get("/api/health")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertFalse(payload["degraded"])
        components = payload["components"]
        for existing in (
            "redis",
            "redis_client",
            "redis_pool",
            "redis_notifications",
            "redis_alerts",
            "cache",
            "provider_quota",
        ):
            self.assertIn(existing, components)
        self.assertEqual("ready", components["qdrant"])
        self.assertEqual("ready", components["rag"])
        self.assertTrue(components["embedding_configured"])


if __name__ == "__main__":
    unittest.main()
