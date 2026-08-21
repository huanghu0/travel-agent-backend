"""把 Redis 运行状态转换为低基数 Prometheus/OpenTelemetry 指标。"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily


@dataclass(frozen=True, slots=True)
class RedisObservabilityConfig:
    """Redis 可观测性开关和告警阈值。"""

    prometheus_enabled: bool = True
    prometheus_path: str = "/metrics"
    otel_enabled: bool = False
    otel_service_name: str = "travel-agent-backend"
    otel_endpoint: str = "http://127.0.0.1:4318/v1/metrics"
    otel_export_interval_seconds: float = 30.0
    alerts_enabled: bool = True
    alert_degraded_after_seconds: float = 30.0
    alert_pool_utilization_threshold: float = 0.8

    @classmethod
    def from_settings(cls, settings: Any) -> "RedisObservabilityConfig":
        path = str(settings.PROMETHEUS_METRICS_PATH or "/metrics").strip()
        if not path.startswith("/"):
            path = f"/{path}"
        threshold = float(settings.REDIS_ALERT_POOL_UTILIZATION_THRESHOLD)
        if not 0 < threshold <= 1:
            raise ValueError("REDIS_ALERT_POOL_UTILIZATION_THRESHOLD 必须在 (0, 1] 范围内")
        return cls(
            prometheus_enabled=bool(settings.PROMETHEUS_METRICS_ENABLED),
            prometheus_path=path,
            otel_enabled=bool(settings.OTEL_METRICS_ENABLED),
            otel_service_name=str(settings.OTEL_SERVICE_NAME),
            otel_endpoint=str(settings.OTEL_EXPORTER_OTLP_METRICS_ENDPOINT),
            otel_export_interval_seconds=max(
                1.0, float(settings.OTEL_METRIC_EXPORT_INTERVAL_SECONDS)
            ),
            alerts_enabled=bool(settings.REDIS_ALERTS_ENABLED),
            alert_degraded_after_seconds=max(
                0.0, float(settings.REDIS_ALERT_DEGRADED_AFTER_SECONDS)
            ),
            alert_pool_utilization_threshold=threshold,
        )


@dataclass(frozen=True, slots=True)
class RedisAlert:
    """可供健康接口、Prometheus Alertmanager 和运维页面消费的告警状态。"""

    code: str
    severity: str
    active: bool
    pending: bool
    duration_seconds: float
    message: str

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class _RedisPrometheusCollector:
    """Prometheus 自定义 Collector；每次抓取只生成一份一致快照。"""

    def __init__(self, snapshot_provider: Callable[[], dict[str, Any]]) -> None:
        self._snapshot_provider = snapshot_provider

    def collect(self):
        snapshot = self._snapshot_provider()
        health = snapshot["health"]
        pool = snapshot["pool"]
        client = snapshot["client_metrics"]
        notifications = snapshot["notification_health"]
        notification_metrics = notifications["metrics"]

        metric = GaugeMetricFamily(
            "travel_agent_redis_up",
            "Redis PING 是否成功。",
        )
        metric.add_metric([], 1 if health.get("healthy") is True else 0)
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_degraded",
            "Redis 是否处于自动降级状态。",
        )
        metric.add_metric([], 1 if health.get("degraded") else 0)
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_health_latency_milliseconds",
            "Redis PING 延迟，单位毫秒。",
        )
        metric.add_metric([], float(health.get("latency_ms") or 0.0))
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_degraded_duration_seconds",
            "Redis 当前连续降级时长。",
        )
        metric.add_metric([], float(snapshot["degraded_duration_seconds"]))
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_pool_connections",
            "Redis 连接池连接数。",
            labels=["state"],
        )
        metric.add_metric(["max"], pool["max_connections"])
        metric.add_metric(["created"], pool["created_connections"])
        metric.add_metric(["in_use"], pool["in_use_connections"])
        metric.add_metric(["available"], pool["available_connections"])
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_pool_utilization_ratio",
            "Redis 连接池使用率。",
        )
        metric.add_metric([], pool["utilization"])
        yield metric

        metric = CounterMetricFamily(
            "travel_agent_redis_client_operations_total",
            "Redis 客户端命令累计结果。",
            labels=["result"],
        )
        metric.add_metric(["success"], client["operation_successes"])
        metric.add_metric(["failure"], client["operation_failures"])
        metric.add_metric(["bypass"], client["operation_bypasses"])
        yield metric

        metric = CounterMetricFamily(
            "travel_agent_redis_health_checks_total",
            "Redis 健康检查累计结果。",
            labels=["result"],
        )
        metric.add_metric(
            ["success"],
            max(0, client["health_checks"] - client["health_check_failures"]),
        )
        metric.add_metric(["failure"], client["health_check_failures"])
        yield metric

        metric = CounterMetricFamily(
            "travel_agent_redis_state_transitions_total",
            "Redis 降级和恢复次数。",
            labels=["transition"],
        )
        metric.add_metric(["degraded"], client["degraded_transitions"])
        metric.add_metric(["recovered"], client["recoveries"])
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_notification_subscriber_running",
            "Redis Pub/Sub 订阅线程是否运行。",
        )
        metric.add_metric([], 1 if notifications["subscriber_running"] else 0)
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_notification_degraded",
            "Redis Pub/Sub 是否降级到数据库轮询。",
        )
        metric.add_metric([], 1 if notifications["degraded"] else 0)
        yield metric

        metric = CounterMetricFamily(
            "travel_agent_redis_notifications_total",
            "Redis 任务通知累计数量。",
            labels=["direction", "kind"],
        )
        for field, direction, kind in (
            ("published_task_available", "published", "task_available"),
            ("published_task_events", "published", "task_event"),
            ("published_cancellations", "published", "cancellation"),
            ("received_task_available", "received", "task_available"),
            ("received_task_events", "received", "task_event"),
            ("received_cancellations", "received", "cancellation"),
        ):
            metric.add_metric([direction, kind], notification_metrics[field])
        yield metric

        metric = CounterMetricFamily(
            "travel_agent_redis_notification_failures_total",
            "Redis 通知发布降级、非法消息和订阅重连次数。",
            labels=["kind"],
        )
        for field in ("publish_degraded", "invalid_messages", "subscriber_reconnects"):
            metric.add_metric([field], notification_metrics[field])
        yield metric

        metric = CounterMetricFamily(
            "travel_agent_task_notification_waits_total",
            "Worker/SSE 通知唤醒和数据库轮询兜底次数。",
            labels=["consumer", "result"],
        )
        metric.add_metric(
            ["worker", "notification"], notification_metrics["worker_notification_wakeups"]
        )
        metric.add_metric(
            ["worker", "poll_timeout"], notification_metrics["worker_poll_timeouts"]
        )
        metric.add_metric(
            ["sse", "notification"], notification_metrics["sse_notification_wakeups"]
        )
        metric.add_metric(
            ["sse", "poll_timeout"], notification_metrics["sse_poll_timeouts"]
        )
        yield metric

        metric = GaugeMetricFamily(
            "travel_agent_redis_alert",
            "Redis 生产告警状态。",
            labels=["code", "severity", "state"],
        )
        for alert in snapshot["alerts"]:
            metric.add_metric(
                [alert["code"], alert["severity"], "active"],
                1 if alert["active"] else 0,
            )
            metric.add_metric(
                [alert["code"], alert["severity"], "pending"],
                1 if alert["pending"] else 0,
            )
        yield metric

        quota = snapshot.get("provider_quota_metrics") or {}
        if quota:
            metric = CounterMetricFamily(
                "travel_agent_provider_quota_checks_total",
                "跨实例供应商请求额度检查结果。",
                labels=["provider", "outcome"],
            )
            providers = sorted(
                set(quota.get("provider_allowed", {}))
                | set(quota.get("provider_rejected", {}))
            )
            for provider in providers:
                metric.add_metric(
                    [provider, "allowed"],
                    quota.get("provider_allowed", {}).get(provider, 0),
                )
                metric.add_metric(
                    [provider, "rejected"],
                    quota.get("provider_rejected", {}).get(provider, 0),
                )
            yield metric

            metric = CounterMetricFamily(
                "travel_agent_provider_quota_degraded_total",
                "Redis 限流故障后的 fail-open/fail-closed 次数。",
                labels=["outcome"],
            )
            metric.add_metric(["allowed"], quota.get("degraded_allowed", 0))
            metric.add_metric(["rejected"], quota.get("degraded_rejected", 0))
            yield metric

        business = snapshot.get("amap_business_cache_metrics") or {}
        domains = business.get("domains", {})
        if domains:
            metric = CounterMetricFamily(
                "travel_agent_amap_business_cache_operations_total",
                "高德天气、景点、酒店和地理编码缓存结果。",
                labels=["domain", "outcome"],
            )
            for domain, values in sorted(domains.items()):
                for outcome in (
                    "hits", "misses", "bypasses", "degraded_reads",
                    "invalid_payloads", "writes", "degraded_writes", "provider_calls",
                ):
                    metric.add_metric([domain, outcome], values.get(outcome, 0))
            yield metric

        metric = GaugeMetricFamily(
            "travel_agent_task_notification_fallback_poll_seconds",
            "Redis 通知丢失时的数据库兜底轮询间隔。",
            labels=["consumer"],
        )
        metric.add_metric(["worker"], snapshot["tuning"]["worker_fallback_poll_seconds"])
        metric.add_metric(["sse"], snapshot["tuning"]["sse_fallback_poll_seconds"])
        yield metric


class RedisRuntimeObservability:
    """统一生成 Redis 健康快照、Prometheus 文本和 OpenTelemetry 指标。"""

    def __init__(
        self,
        *,
        config: RedisObservabilityConfig,
        client_manager: Any,
        notification_bus: Any,
        cache_store: Any,
        route_cache: Any = None,
        restaurant_cache: Any = None,
        business_cache: Any = None,
        rate_limiter: Any = None,
        worker: Any = None,
        worker_fallback_poll_seconds: float = 5.0,
        sse_fallback_poll_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.client_manager = client_manager
        self.notification_bus = notification_bus
        self.cache_store = cache_store
        self.route_cache = route_cache
        self.restaurant_cache = restaurant_cache
        self.business_cache = business_cache
        self.rate_limiter = rate_limiter
        self.worker = worker
        self.worker_fallback_poll_seconds = worker_fallback_poll_seconds
        self.sse_fallback_poll_seconds = sse_fallback_poll_seconds
        self._monotonic = monotonic
        self._alert_since: dict[str, float] = {}
        self._lock = threading.RLock()
        self._cached_at = 0.0
        self._cached_snapshot: dict[str, Any] | None = None
        self._otel_provider: Any = None
        self.registry = CollectorRegistry(auto_describe=False)
        self.registry.register(_RedisPrometheusCollector(self.snapshot))

    def _alerts(self, health: dict[str, Any], notification: dict[str, Any], pool: dict[str, Any]):
        if not self.config.alerts_enabled:
            return []
        conditions = (
            (
                "redis_unavailable",
                "critical",
                bool(health.get("degraded")),
                "Redis 不可用，缓存和任务通知已回退持久化后端。",
            ),
            (
                "redis_notification_degraded",
                "warning",
                bool(notification.get("degraded")),
                "Redis Pub/Sub 订阅异常，Worker/SSE 正在使用数据库轮询。",
            ),
            (
                "redis_pool_near_capacity",
                "warning",
                float(pool.get("utilization", 0.0))
                >= self.config.alert_pool_utilization_threshold,
                "Redis 连接池使用率达到告警阈值。",
            ),
        )
        now = self._monotonic()
        alerts = []
        for code, severity, triggered, message in conditions:
            if triggered:
                since = self._alert_since.setdefault(code, now)
                duration = max(0.0, now - since)
            else:
                self._alert_since.pop(code, None)
                duration = 0.0
            active = triggered and duration >= self.config.alert_degraded_after_seconds
            alerts.append(
                RedisAlert(
                    code=code,
                    severity=severity,
                    active=active,
                    pending=triggered and not active,
                    duration_seconds=round(duration, 3),
                    message=message,
                ).model_dump()
            )
        return alerts

    def snapshot(self) -> dict[str, Any]:
        """执行一次轻量 PING 并返回一致、脱敏、低基数的运行时快照。"""

        health = self.client_manager.check_health().model_dump()
        client_metrics = self.client_manager.metrics_snapshot().model_dump()
        pool = self.client_manager.pool_snapshot().model_dump()
        notification = self.notification_bus.health_snapshot().model_dump()
        cache = self.cache_store.metrics_snapshot().model_dump()
        route = self.route_cache.metrics_snapshot().model_dump() if self.route_cache else None
        restaurant = (
            self.restaurant_cache.metrics_snapshot().model_dump()
            if self.restaurant_cache
            else None
        )
        business = (
            self.business_cache.metrics_snapshot().model_dump()
            if self.business_cache
            else None
        )
        quota = (
            self.rate_limiter.metrics_snapshot().model_dump()
            if self.rate_limiter
            else None
        )
        with self._lock:
            alerts = self._alerts(health, notification, pool)
        return {
            "health": health,
            "degraded_duration_seconds": round(
                self.client_manager.degraded_duration_seconds(), 3
            ),
            "client_metrics": client_metrics,
            "pool": pool,
            "notification_health": notification,
            "cache_metrics": cache,
            "layered_cache_metrics": {
                "route": route,
                "restaurant": restaurant,
            },
            "amap_business_cache_metrics": business,
            "provider_quota_metrics": quota,
            "worker_running": bool(self.worker and self.worker.running),
            "tuning": {
                "worker_fallback_poll_seconds": self.worker_fallback_poll_seconds,
                "sse_fallback_poll_seconds": self.sse_fallback_poll_seconds,
                "pool_utilization_alert_threshold": (
                    self.config.alert_pool_utilization_threshold
                ),
            },
            "alerts": alerts,
            "degraded": bool(
                health.get("degraded")
                or notification.get("degraded")
                or any(item["active"] for item in alerts)
            ),
        }

    def cached_snapshot(self, max_age_seconds: float = 1.0) -> dict[str, Any]:
        """OpenTelemetry 多个回调共享短时缓存，避免一次采集重复 PING。"""

        now = self._monotonic()
        with self._lock:
            if self._cached_snapshot is not None and now - self._cached_at <= max_age_seconds:
                return self._cached_snapshot
        snapshot = self.snapshot()
        with self._lock:
            self._cached_at = now
            self._cached_snapshot = snapshot
        return snapshot

    def prometheus_payload(self) -> tuple[bytes, str]:
        """返回 Prometheus 文本协议响应。"""

        if not self.config.prometheus_enabled:
            raise RuntimeError("Prometheus 指标端点未启用")
        return generate_latest(self.registry), CONTENT_TYPE_LATEST

    def start(self) -> None:
        """按配置启动 OpenTelemetry OTLP 定时导出；默认关闭。"""

        if not self.config.otel_enabled or self._otel_provider is not None:
            return
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.metrics import Observation
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        exporter = OTLPMetricExporter(endpoint=self.config.otel_endpoint)
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=int(self.config.otel_export_interval_seconds * 1000),
        )
        provider = MeterProvider(
            resource=Resource.create({"service.name": self.config.otel_service_name}),
            metric_readers=[reader],
        )
        meter = provider.get_meter("travel-agent.redis")

        def gauge(path: tuple[str, ...]):
            def callback(_options):
                value: Any = self.cached_snapshot()
                for part in path:
                    value = value[part]
                return [Observation(float(value))]
            return callback

        meter.create_observable_gauge(
            "travel_agent.redis.up",
            callbacks=[lambda options: [Observation(1.0 if self.cached_snapshot()["health"].get("healthy") is True else 0.0)]],
            description="Redis PING 是否成功",
        )
        meter.create_observable_gauge(
            "travel_agent.redis.degraded",
            callbacks=[lambda options: [Observation(1.0 if self.cached_snapshot()["health"].get("degraded") else 0.0)]],
            description="Redis 是否处于降级状态",
        )
        meter.create_observable_gauge(
            "travel_agent.redis.health_latency_ms",
            callbacks=[gauge(("health", "latency_ms"))],
            unit="ms",
        )
        meter.create_observable_gauge(
            "travel_agent.redis.pool.utilization",
            callbacks=[gauge(("pool", "utilization"))],
        )
        meter.create_observable_gauge(
            "travel_agent.redis.notification.degraded",
            callbacks=[lambda options: [Observation(1.0 if self.cached_snapshot()["notification_health"].get("degraded") else 0.0)]],
        )
        self._otel_provider = provider

    def stop(self) -> None:
        """刷新并关闭 OpenTelemetry 后台导出线程。"""

        provider = self._otel_provider
        self._otel_provider = None
        if provider is not None:
            provider.shutdown()
