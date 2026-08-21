"""Redis 并发压力测试、连接池容量和通知轮询间隔调优。"""

from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.infrastructure.notifications.models import TaskNotificationMessage
from app.infrastructure.redis import (
    RedisClientManager,
    RedisConfig,
    RedisTaskNotificationBus,
)


@dataclass(frozen=True, slots=True)
class RedisTuningRecommendation:
    """根据压力测试结果生成配置建议；不会自动修改生产环境变量。"""

    current_max_connections: int
    recommended_max_connections: int
    observed_peak_in_use_connections: int
    pool_headroom_ratio: float
    worker_fallback_poll_seconds: float
    sse_fallback_poll_seconds: float
    estimated_mysql_fallback_polls_per_minute: float
    rationale: tuple[str, ...]

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rationale"] = list(self.rationale)
        return payload


@dataclass(frozen=True, slots=True)
class RedisLoadTestReport:
    """Redis 压力测试脱敏报告，不包含实际 Key 和消息正文。"""

    started_at: str
    redis_target: str
    concurrency: int
    operations_per_worker: int
    command_count: int
    duration_seconds: float
    throughput_commands_per_second: float
    success_count: int
    error_count: int
    latency_ms_p50: float
    latency_ms_p95: float
    latency_ms_p99: float
    peak_in_use_connections: int
    max_connections: int
    notification_count: int
    notification_received: int
    notification_receive_ratio: float
    notification_end_to_end_latency_ms_p50: float
    notification_end_to_end_latency_ms_p95: float
    notification_end_to_end_latency_ms_p99: float
    passed: bool
    thresholds: dict[str, float]
    tuning: RedisTuningRecommendation
    errors: tuple[str, ...] = ()

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["errors"] = list(self.errors)
        payload["tuning"] = self.tuning.model_dump()
        return payload


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def recommend_redis_runtime(
    *,
    current_max_connections: int,
    concurrency: int,
    peak_in_use_connections: int,
    notification_receive_ratio: float,
    notification_latency_p95_ms: float,
    worker_processes: int,
    expected_sse_connections: int,
) -> RedisTuningRecommendation:
    """用保守公式给出单实例连接池和 MySQL 兜底轮询建议。"""

    # 每个并发 Redis 命令最多占用一个连接；额外预留 Pub/Sub、健康检查和 25% 抖动空间。
    demand = max(concurrency, peak_in_use_connections) + 2
    recommended_pool = max(10, math.ceil(demand * 1.25))
    headroom = max(0.0, (recommended_pool - peak_in_use_connections) / recommended_pool)

    if notification_receive_ratio >= 0.999 and notification_latency_p95_ms <= 50:
        worker_poll, sse_poll = 5.0, 5.0
        reliability_reason = "通知接收率和端到端延迟良好，可使用 5 秒低频数据库兜底。"
    elif notification_receive_ratio >= 0.99 and notification_latency_p95_ms <= 200:
        worker_poll, sse_poll = 3.0, 5.0
        reliability_reason = "通知基本可靠，Worker 使用 3 秒、SSE 使用 5 秒兜底。"
    else:
        worker_poll, sse_poll = 1.0, 2.0
        reliability_reason = "通知可靠性不足，暂时缩短数据库兜底间隔并触发告警。"

    estimated_polls = (
        max(0, worker_processes) * 60 / worker_poll
        + max(0, expected_sse_connections) * 60 / sse_poll
    )
    rationale = (
        f"连接池按并发需求 {demand} 加 25% 余量计算。",
        reliability_reason,
        "SSE 不独占 Redis 连接，但会增加数据库兜底查询，因此单独估算轮询成本。",
    )
    return RedisTuningRecommendation(
        current_max_connections=current_max_connections,
        recommended_max_connections=recommended_pool,
        observed_peak_in_use_connections=peak_in_use_connections,
        pool_headroom_ratio=round(headroom, 4),
        worker_fallback_poll_seconds=worker_poll,
        sse_fallback_poll_seconds=sse_poll,
        estimated_mysql_fallback_polls_per_minute=round(estimated_polls, 2),
        rationale=rationale,
    )


def run_redis_load_test(
    *,
    redis_config: RedisConfig,
    concurrency: int = 20,
    operations_per_worker: int = 50,
    notification_count: int = 200,
    max_p95_latency_ms: float = 100.0,
    max_notification_p95_latency_ms: float = 200.0,
    minimum_notification_receive_ratio: float = 0.99,
    worker_processes: int = 2,
    expected_sse_connections: int = 50,
) -> RedisLoadTestReport:
    """并发执行 SET/GET/DELETE 和 Pub/Sub，不调用 MySQL、高德或 LLM。"""

    if not redis_config.enabled:
        raise RuntimeError("REDIS_ENABLED=false，无法执行 Redis 压力测试")
    if concurrency < 1 or operations_per_worker < 1 or notification_count < 1:
        raise ValueError("并发数、每 Worker 操作数和通知数必须大于 0")

    batch = uuid4().hex
    manager = RedisClientManager(redis_config)
    latencies: list[float] = []
    notification_latencies: list[float] = []
    notification_sent_at: dict[str, float] = {}
    notification_received_ids: set[str] = set()
    errors: list[str] = []
    result_lock = threading.Lock()
    notification_condition = threading.Condition(result_lock)
    peak_in_use = 0
    sampler_stop = threading.Event()
    notification_received = 0
    duration = 0.0

    def observe_message(message: TaskNotificationMessage) -> None:
        """在订阅线程收到消息时记录真实的 publish→receive 端到端延迟。"""

        if message.kind != "task_event":
            return
        with notification_condition:
            sent_at = notification_sent_at.get(message.task_id)
            if sent_at is None or message.task_id in notification_received_ids:
                return
            notification_received_ids.add(message.task_id)
            notification_latencies.append((time.perf_counter() - sent_at) * 1000)
            notification_condition.notify_all()

    bus = RedisTaskNotificationBus(
        client_manager=manager,
        key_builder=manager.key_builder,
        enabled=True,
        reconnect_delay_seconds=0.1,
        message_observer=observe_message,
    )

    health = manager.check_health()
    if not health.healthy:
        manager.close()
        raise RuntimeError(f"Redis 不可用: {health.error}")

    client = manager.get_client(force_retry=True)
    if client is None:
        manager.close()
        raise RuntimeError("Redis 客户端初始化失败")

    def sample_pool() -> None:
        nonlocal peak_in_use
        while not sampler_stop.wait(0.005):
            current = manager.pool_snapshot().in_use_connections
            with result_lock:
                peak_in_use = max(peak_in_use, current)

    def execute_worker(worker_index: int) -> tuple[int, int]:
        successes = failures = 0
        for operation_index in range(operations_per_worker):
            key = manager.key_builder.literal(
                "loadtest",
                f"{batch}-{worker_index}-{operation_index}",
            )
            started = time.perf_counter()
            try:
                value = f"value-{operation_index}".encode("utf-8")
                client.set(key, value, ex=60)
                loaded = client.get(key)
                if loaded != value:
                    raise AssertionError("Redis GET 返回值不一致")
                client.delete(key)
                successes += 1
            except Exception as exc:
                failures += 1
                with result_lock:
                    if len(errors) < 20:
                        errors.append(f"{exc.__class__.__name__}: {exc}")
            finally:
                latency = (time.perf_counter() - started) * 1000
                with result_lock:
                    latencies.append(latency)
        return successes, failures

    sampler = threading.Thread(
        target=sample_pool,
        name="redis-load-pool-sampler",
        daemon=True,
    )
    sampler.start()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    started = time.perf_counter()
    success_count = error_count = 0
    try:
        bus.start()
        # Pub/Sub 频道必须先完成订阅，否则 Redis 的可丢失消息会在启动窗口内消失。
        channels = (bus.task_channel, bus.event_channel, bus.cancellation_channel)
        subscriber_deadline = time.monotonic() + 5.0
        while time.monotonic() < subscriber_deadline:
            subscribers = manager.execute(
                lambda redis: redis.pubsub_numsub(*channels),
                fallback=(),
            )
            if subscribers and all(int(count) >= 1 for _, count in subscribers):
                break
            time.sleep(0.02)
        else:
            raise TimeoutError("Redis 压力测试 Pub/Sub 订阅未就绪")

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(execute_worker, index) for index in range(concurrency)
            ]
            for future in as_completed(futures):
                success, failure = future.result()
                success_count += success
                error_count += failure

        for index in range(notification_count):
            task_id = f"load-{batch[:12]}-{index}"
            with result_lock:
                notification_sent_at[task_id] = time.perf_counter()
            bus.publish_task_event(task_id, event_id=index + 1, event_type="load_test")

        deadline = time.monotonic() + 10.0
        with notification_condition:
            while len(notification_received_ids) < notification_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                notification_condition.wait(timeout=min(remaining, 0.25))
            notification_received = len(notification_received_ids)
    finally:
        duration = max(0.000001, time.perf_counter() - started)
        sampler_stop.set()
        sampler.join(timeout=1.0)
        bus.stop()
        peak_in_use = max(peak_in_use, manager.pool_snapshot().in_use_connections)
        manager.close()

    command_count = concurrency * operations_per_worker * 3
    receive_ratio = min(1.0, notification_received / notification_count)
    notification_p95 = _percentile(notification_latencies, 0.95)
    tuning = recommend_redis_runtime(
        current_max_connections=redis_config.max_connections,
        concurrency=concurrency,
        peak_in_use_connections=peak_in_use,
        notification_receive_ratio=receive_ratio,
        notification_latency_p95_ms=notification_p95,
        worker_processes=worker_processes,
        expected_sse_connections=expected_sse_connections,
    )
    p95 = _percentile(latencies, 0.95)
    passed = (
        error_count == 0
        and p95 <= max_p95_latency_ms
        and receive_ratio >= minimum_notification_receive_ratio
        and notification_p95 <= max_notification_p95_latency_ms
    )
    return RedisLoadTestReport(
        started_at=started_at,
        redis_target=redis_config.safe_target(),
        concurrency=concurrency,
        operations_per_worker=operations_per_worker,
        command_count=command_count,
        duration_seconds=round(duration, 3),
        throughput_commands_per_second=round(command_count / duration, 2),
        success_count=success_count,
        error_count=error_count,
        latency_ms_p50=_percentile(latencies, 0.50),
        latency_ms_p95=p95,
        latency_ms_p99=_percentile(latencies, 0.99),
        peak_in_use_connections=peak_in_use,
        max_connections=redis_config.max_connections,
        notification_count=notification_count,
        notification_received=notification_received,
        notification_receive_ratio=round(receive_ratio, 4),
        notification_end_to_end_latency_ms_p50=_percentile(
            notification_latencies, 0.50
        ),
        notification_end_to_end_latency_ms_p95=notification_p95,
        notification_end_to_end_latency_ms_p99=_percentile(
            notification_latencies, 0.99
        ),
        passed=passed,
        thresholds={
            "max_p95_latency_ms": max_p95_latency_ms,
            "max_notification_p95_latency_ms": max_notification_p95_latency_ms,
            "minimum_notification_receive_ratio": minimum_notification_receive_ratio,
        },
        tuning=tuning,
        errors=tuple(errors),
    )


def write_redis_load_report(report: RedisLoadTestReport, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output
