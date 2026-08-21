import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.evaluation.redis_load_test import (
    RedisLoadTestReport,
    RedisTuningRecommendation,
    recommend_redis_runtime,
    write_redis_load_report,
)
from app.observability.redis_metrics import (
    RedisObservabilityConfig,
    RedisRuntimeObservability,
)


class _Dump:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return dict(self.payload)


class _Manager:
    def __init__(self, *, degraded=False, utilization=0.25):
        self.degraded = degraded
        self.utilization = utilization

    def check_health(self):
        return _Dump(
            {
                "enabled": True,
                "status": "degraded" if self.degraded else "ok",
                "target": "redis://127.0.0.1:6379/0",
                "healthy": not self.degraded,
                "degraded": self.degraded,
                "latency_ms": 1.25,
                "error": "offline" if self.degraded else None,
            }
        )

    def metrics_snapshot(self):
        return _Dump(
            {
                "operation_requests": 10,
                "operation_successes": 9,
                "operation_failures": 1,
                "operation_bypasses": 0,
                "health_checks": 2,
                "health_check_failures": int(self.degraded),
                "degraded_transitions": int(self.degraded),
                "recoveries": 0,
            }
        )

    def pool_snapshot(self):
        return _Dump(
            {
                "max_connections": 20,
                "created_connections": 5,
                "in_use_connections": int(20 * self.utilization),
                "available_connections": 1,
                "utilization": self.utilization,
            }
        )

    def degraded_duration_seconds(self):
        return 35.0 if self.degraded else 0.0


class _Bus:
    def __init__(self, *, degraded=False):
        self.degraded = degraded

    def health_snapshot(self):
        return _Dump(
            {
                "enabled": True,
                "backend": "redis-pubsub+local+database-polling",
                "subscriber_running": not self.degraded,
                "degraded": self.degraded,
                "channels": [],
                "metrics": {
                    "published_task_available": 1,
                    "published_task_events": 2,
                    "published_cancellations": 3,
                    "publish_degraded": int(self.degraded),
                    "received_task_available": 1,
                    "received_task_events": 2,
                    "received_cancellations": 3,
                    "invalid_messages": 0,
                    "subscriber_reconnects": 1,
                    "worker_notification_wakeups": 4,
                    "worker_poll_timeouts": 5,
                    "sse_notification_wakeups": 6,
                    "sse_poll_timeouts": 7,
                    "cancel_fast_path_hits": 2,
                    "mysql_poll_fallbacks": 1,
                },
            }
        )


class RedisObservabilityTests(unittest.TestCase):
    def make_observability(self, *, manager=None, bus=None, clock=None, alert_after=30):
        return RedisRuntimeObservability(
            config=RedisObservabilityConfig(
                alert_degraded_after_seconds=alert_after,
                alert_pool_utilization_threshold=0.8,
            ),
            client_manager=manager or _Manager(),
            notification_bus=bus or _Bus(),
            cache_store=SimpleNamespace(metrics_snapshot=lambda: _Dump({"hits": 1})),
            worker_fallback_poll_seconds=5,
            sse_fallback_poll_seconds=5,
            monotonic=clock or (lambda: 0.0),
        )

    def test_snapshot_contains_pool_notifications_and_tuning(self):
        snapshot = self.make_observability().snapshot()

        self.assertFalse(snapshot["degraded"])
        self.assertEqual(20, snapshot["pool"]["max_connections"])
        self.assertEqual(5, snapshot["tuning"]["worker_fallback_poll_seconds"])
        self.assertEqual(2, snapshot["notification_health"]["metrics"]["received_task_events"])

    def test_degraded_alert_changes_from_pending_to_active(self):
        now = [10.0]
        observability = self.make_observability(
            manager=_Manager(degraded=True),
            bus=_Bus(degraded=True),
            clock=lambda: now[0],
            alert_after=30,
        )

        first = observability.snapshot()
        now[0] = 41.0
        second = observability.snapshot()

        self.assertTrue(any(alert["pending"] for alert in first["alerts"]))
        self.assertTrue(any(alert["active"] for alert in second["alerts"]))
        self.assertTrue(second["degraded"])

    def test_prometheus_payload_uses_low_cardinality_metric_names(self):
        payload, content_type = self.make_observability().prometheus_payload()
        text = payload.decode("utf-8")

        self.assertIn("text/plain", content_type)
        self.assertIn("travel_agent_redis_up", text)
        self.assertIn("travel_agent_redis_pool_utilization_ratio", text)
        self.assertNotIn("task_id", text)

    def test_otel_enabled_builds_otlp_pipeline(self):
        observability = RedisRuntimeObservability(
            config=RedisObservabilityConfig(
                otel_enabled=True,
                otel_endpoint="http://collector:4318/v1/metrics",
                otel_export_interval_seconds=12,
            ),
            client_manager=_Manager(),
            notification_bus=_Bus(),
            cache_store=SimpleNamespace(metrics_snapshot=lambda: _Dump({"hits": 1})),
        )
        provider = Mock()
        meter = Mock()
        provider.get_meter.return_value = meter

        with patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as exporter_cls, patch(
            "opentelemetry.sdk.metrics.export.PeriodicExportingMetricReader"
        ) as reader_cls, patch(
            "opentelemetry.sdk.metrics.MeterProvider", return_value=provider
        ) as provider_cls, patch(
            "opentelemetry.sdk.resources.Resource.create", return_value="resource"
        ):
            observability.start()

        exporter_cls.assert_called_once_with(
            endpoint="http://collector:4318/v1/metrics"
        )
        reader_cls.assert_called_once_with(
            exporter_cls.return_value, export_interval_millis=12000
        )
        provider_cls.assert_called_once_with(
            resource="resource", metric_readers=[reader_cls.return_value]
        )
        self.assertEqual(5, meter.create_observable_gauge.call_count)

        observability.stop()
        provider.shutdown.assert_called_once_with()

    def test_otel_disabled_start_and_stop_are_idempotent(self):
        observability = self.make_observability()

        observability.start()
        observability.stop()
        observability.stop()

        self.assertIsNone(observability._otel_provider)

    def test_otel_stop_shuts_down_existing_provider_once(self):
        observability = self.make_observability()
        provider = Mock()
        observability._otel_provider = provider

        observability.stop()
        observability.stop()

        provider.shutdown.assert_called_once_with()



class RedisTuningTests(unittest.TestCase):
    def test_reliable_notifications_allow_slow_database_fallback(self):
        result = recommend_redis_runtime(
            current_max_connections=20,
            concurrency=20,
            peak_in_use_connections=16,
            notification_receive_ratio=1.0,
            notification_latency_p95_ms=10,
            worker_processes=2,
            expected_sse_connections=50,
        )

        self.assertGreaterEqual(result.recommended_max_connections, 28)
        self.assertEqual(5.0, result.worker_fallback_poll_seconds)
        self.assertEqual(5.0, result.sse_fallback_poll_seconds)

    def test_unreliable_notifications_shorten_fallback_interval(self):
        result = recommend_redis_runtime(
            current_max_connections=20,
            concurrency=10,
            peak_in_use_connections=5,
            notification_receive_ratio=0.8,
            notification_latency_p95_ms=500,
            worker_processes=1,
            expected_sse_connections=10,
        )

        self.assertEqual(1.0, result.worker_fallback_poll_seconds)
        self.assertEqual(2.0, result.sse_fallback_poll_seconds)

    def test_load_report_is_written_as_json(self):
        tuning = RedisTuningRecommendation(
            current_max_connections=20,
            recommended_max_connections=28,
            observed_peak_in_use_connections=10,
            pool_headroom_ratio=0.64,
            worker_fallback_poll_seconds=5,
            sse_fallback_poll_seconds=5,
            estimated_mysql_fallback_polls_per_minute=100,
            rationale=("safe",),
        )
        report = RedisLoadTestReport(
            started_at="2026-08-21T12:00:00+0800",
            redis_target="redis://127.0.0.1:6379/0",
            concurrency=20,
            operations_per_worker=10,
            command_count=600,
            duration_seconds=1,
            throughput_commands_per_second=600,
            success_count=200,
            error_count=0,
            latency_ms_p50=1,
            latency_ms_p95=2,
            latency_ms_p99=3,
            peak_in_use_connections=10,
            max_connections=20,
            notification_count=10,
            notification_received=10,
            notification_receive_ratio=1,
            notification_end_to_end_latency_ms_p50=1,
            notification_end_to_end_latency_ms_p95=2,
            notification_end_to_end_latency_ms_p99=3,
            passed=True,
            thresholds={"max_p95_latency_ms": 100},
            tuning=tuning,
        )
        with tempfile.TemporaryDirectory() as tempdir:
            output = write_redis_load_report(report, Path(tempdir) / "load.json")
            text = output.read_text(encoding="utf-8")

        self.assertIn('"passed": true', text)
        self.assertIn('"recommended_max_connections": 28', text)


if __name__ == "__main__":
    unittest.main()
