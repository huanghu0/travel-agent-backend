"""Redis 任务运行时的真实跨进程验收。

该模块只使用 MySQL 测试库和独立 Redis Key 前缀，不调用高德或 LLM。
Redis 仍然只是通知/加速层，任务、事件、取消与租约事实均从 MySQL 验证。
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from queue import Empty
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import delete, select

from app.core.config import settings
from app.infrastructure.redis import (
    RedisClientManager,
    RedisConfig,
    RedisTaskNotificationBus,
)
from app.persistence.database import (
    MySQLDatabaseConfig,
    check_mysql_health,
    create_mysql_engine,
)
from app.persistence.mysql_trip_task_store import MySQLTripTaskStore
from app.persistence.sqlalchemy_models import TripPlanningTaskRow, TripTaskEventRow
from app.schemas.trip_schema import TripRequest
from app.task_runtime.notifying_store import NotifyingTripTaskStore


@dataclass(frozen=True, slots=True)
class RedisRuntimeAcceptanceCase:
    """单项验收结果；details 只记录技术指标，不记录用户输入或密钥。"""

    name: str
    passed: bool
    duration_ms: float
    details: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RedisRuntimeAcceptanceReport:
    """完整 Redis 任务运行时验收报告。"""

    started_at: str
    database: str
    redis_target: str
    cases: tuple[RedisRuntimeAcceptanceCase, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        payload["passed_cases"] = sum(case.passed for case in self.cases)
        payload["total_cases"] = len(self.cases)
        return payload


class _DummyWorker:
    """SSE 验收期间禁用真实编排 Worker，避免调用高德或 LLM。"""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def wake(self) -> None:
        return None


def _request(index: int) -> TripRequest:
    """构造互不重复的确定性任务，避免活动任务指纹去重。"""

    return TripRequest(
        city=f"验收城市{index}",
        start_date="2026-08-21",
        end_date="2026-08-21",
        travel_days=1,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=[f"redis-acceptance-{index}"],
        free_text_input="",
    )


def _wait_until(predicate: Callable[[], bool], timeout_seconds: float, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _queue_get(queue, timeout_seconds: float) -> Any:
    try:
        return queue.get(timeout=timeout_seconds)
    except Empty as exc:
        raise TimeoutError("等待跨进程验收结果超时") from exc


def _cleanup_acceptance_tasks(engine, idempotency_prefix: str) -> None:
    """只删除本次唯一前缀创建的测试任务，绝不清空整个测试库。"""

    pattern = f"{idempotency_prefix}%"
    task_ids = select(TripPlanningTaskRow.task_id).where(
        TripPlanningTaskRow.idempotency_key.like(pattern)
    )
    with engine.begin() as connection:
        connection.execute(
            delete(TripTaskEventRow).where(TripTaskEventRow.task_id.in_(task_ids))
        )
        connection.execute(
            delete(TripPlanningTaskRow).where(
                TripPlanningTaskRow.idempotency_key.like(pattern)
            )
        )


def _redis_bus(config: RedisConfig) -> tuple[RedisClientManager, RedisTaskNotificationBus]:
    manager = RedisClientManager(config)
    bus = RedisTaskNotificationBus(
        client_manager=manager,
        key_builder=manager.key_builder,
        enabled=True,
        reconnect_delay_seconds=0.1,
    )
    return manager, bus


def _subscribers_ready(manager: RedisClientManager, channels: tuple[str, ...], count: int = 1) -> bool:
    values = manager.execute(
        lambda client: client.pubsub_numsub(*channels),
        fallback=(),
    )
    if not values:
        return False
    counts = {str(channel.decode() if isinstance(channel, bytes) else channel): int(value) for channel, value in values}
    return all(counts.get(channel, 0) >= count for channel in channels)


def _pubsub_observer_process(config: RedisConfig, task_id: str, ready, result_queue) -> None:
    manager, bus = _redis_bus(config)
    try:
        bus.start()
        if not _wait_until(lambda: bus.subscriber_running, 3.0):
            raise RuntimeError("Redis 订阅线程未启动")
        ready.set()
        received = _wait_until(
            lambda: (
                bus.metrics_snapshot().received_task_available >= 1
                and bus.metrics_snapshot().received_task_events >= 1
                and bus.metrics_snapshot().received_cancellations >= 1
                and bus.is_cancel_signalled(task_id)
            ),
            8.0,
        )
        metrics = bus.metrics_snapshot().model_dump()
        result_queue.put(
            {
                "ok": received,
                "metrics": metrics,
                "cancel_signalled": bus.is_cancel_signalled(task_id),
            }
        )
    except Exception as exc:
        result_queue.put({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
    finally:
        bus.stop()
        manager.close()


def _claim_one_process(mysql_config: MySQLDatabaseConfig, worker_id: str, start, ready, result_queue) -> None:
    engine = create_mysql_engine(mysql_config)
    store = MySQLTripTaskStore(engine)
    try:
        ready.put(worker_id)
        if not start.wait(8.0):
            raise TimeoutError("等待多 Worker 同步启动超时")
        # MySQL SKIP LOCKED 在竞争最早记录时允许某一轮返回空；真实 Worker
        # 会继续下一轮领取，因此验收也采用短间隔重试，而不是把一次空结果误判为失败。
        task = None
        deadline = time.monotonic() + 4.0
        claim_attempts = 0
        while task is None and time.monotonic() < deadline:
            claim_attempts += 1
            task = store.claim_next(worker_id, lease_seconds=10.0)
            if task is None:
                time.sleep(0.05)
        if task is None:
            result_queue.put({"ok": False, "worker_id": worker_id, "error": "重试后仍未领取到任务", "claim_attempts": claim_attempts})
            return
        # 保持短暂租约重叠窗口，确保三个进程真实并发持有不同记录。
        time.sleep(0.2)
        store.mark_succeeded(task.task_id, worker_id, session_id=task.session_id)
        result_queue.put({"ok": True, "worker_id": worker_id, "task_id": task.task_id, "claim_attempts": claim_attempts})
    except Exception as exc:
        result_queue.put(
            {"ok": False, "worker_id": worker_id, "error": f"{exc.__class__.__name__}: {exc}"}
        )
    finally:
        engine.dispose()


def _cancel_and_finish_process(
    mysql_config: MySQLDatabaseConfig,
    redis_config: RedisConfig,
    task_id: str,
    worker_id: str,
    start,
    result_queue,
) -> None:
    if not start.wait(8.0):
        result_queue.put({"ok": False, "error": "等待 SSE 建立超时"})
        return
    engine = create_mysql_engine(mysql_config)
    manager, bus = _redis_bus(redis_config)
    store = NotifyingTripTaskStore(
        delegate=MySQLTripTaskStore(engine),
        notification_bus=bus,
    )
    try:
        cancelled = store.request_cancel(task_id)
        # 人工重复发布唤醒消息，验证 SSE 仍只根据数据库 event_id 展示一次业务事件。
        bus.publish_task_event(task_id, event_type="cancellation_requested")
        bus.publish_task_event(task_id, event_type="cancellation_requested")
        finished = store.mark_cancelled(
            task_id,
            worker_id,
            message="跨进程取消验收完成",
        )
        result_queue.put(
            {
                "ok": True,
                "cancel_requested": cancelled.cancel_requested,
                "status": finished.status,
            }
        )
    except Exception as exc:
        result_queue.put({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
    finally:
        bus.stop()
        manager.close()
        engine.dispose()


def _find_redis_server(explicit_path: str | None = None) -> str:
    candidates = [explicit_path, shutil.which("redis-server")]
    if os.name == "nt":
        candidates.extend([r"D:\redis\redis-server.exe", r"C:\redis\redis-server.exe"])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("未找到 redis-server；无法执行真实故障/恢复验收")


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_temporary_redis(executable: str, port: int) -> subprocess.Popen:
    command = [
        executable,
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--save",
        "",
        "--appendonly",
        "no",
    ]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )


def _stop_temporary_redis(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3.0)


def _run_case(name: str, callback: Callable[[], dict[str, Any]]) -> RedisRuntimeAcceptanceCase:
    started = time.perf_counter()
    try:
        details = callback()
        passed = bool(details.pop("passed", True))
        return RedisRuntimeAcceptanceCase(
            name=name,
            passed=passed,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            details=details,
            error=None if passed else str(details.get("failure", "验收断言未通过")),
        )
    except Exception as exc:
        return RedisRuntimeAcceptanceCase(
            name=name,
            passed=False,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            details={},
            error=f"{exc.__class__.__name__}: {exc}",
        )


def run_redis_runtime_acceptance(
    *,
    mysql_database: str | None = None,
    redis_server_path: str | None = None,
) -> RedisRuntimeAcceptanceReport:
    """执行四组真实验收，失败项会进入结构化报告而不会中断其余场景。"""

    database = mysql_database or settings.MYSQL_TEST_DATABASE
    if database == settings.MYSQL_DATABASE:
        raise ValueError("Redis 运行时验收拒绝使用 MYSQL_DATABASE，请使用独立 MYSQL_TEST_DATABASE")

    mysql_config = MySQLDatabaseConfig.from_settings(settings, database=database)
    engine = create_mysql_engine(mysql_config)
    health = check_mysql_health(engine, mysql_config)
    if not health.healthy:
        engine.dispose()
        raise RuntimeError(f"MySQL 测试库不可用: {health.error}")

    base_redis_config = RedisConfig.from_settings(settings)
    if not base_redis_config.enabled:
        engine.dispose()
        raise RuntimeError("REDIS_ENABLED=false，无法执行 Redis 真实验收")
    redis_health_manager = RedisClientManager(base_redis_config)
    redis_health = redis_health_manager.check_health()
    redis_health_manager.close()
    if not redis_health.healthy:
        engine.dispose()
        raise RuntimeError(f"Redis 不可用: {redis_health.error}")

    batch = f"redis-acceptance-{uuid4().hex}"
    redis_config = replace(
        base_redis_config,
        key_prefix=f"{base_redis_config.key_prefix}:acceptance:{uuid4().hex[:12]}",
        degrade_cooldown_seconds=0.1,
        socket_connect_timeout_seconds=0.5,
        socket_timeout_seconds=1.0,
    )
    context = multiprocessing.get_context("spawn")
    cases: list[RedisRuntimeAcceptanceCase] = []

    try:
        # 清理此前被中断验收遗留的同类任务；仅作用于独立测试库和固定验收前缀。
        _cleanup_acceptance_tasks(engine, "redis-acceptance-")

        def pubsub_case() -> dict[str, Any]:
            task_id = f"task-{uuid4().hex}"
            ready = context.Event()
            queue = context.Queue()
            process = context.Process(
                target=_pubsub_observer_process,
                args=(redis_config, task_id, ready, queue),
                name="redis-pubsub-acceptance-observer",
            )
            process.start()
            manager, publisher = _redis_bus(redis_config)
            try:
                if not ready.wait(5.0):
                    raise TimeoutError("跨进程 Redis 订阅器未就绪")
                channels = (
                    publisher.task_channel,
                    publisher.event_channel,
                    publisher.cancellation_channel,
                )
                if not _wait_until(lambda: _subscribers_ready(manager, channels), 5.0):
                    raise TimeoutError("Redis Pub/Sub 频道订阅未就绪")
                publisher.publish_task_available(task_id)
                publisher.publish_task_event(task_id, event_id=1, event_type="task_started")
                publisher.publish_cancellation(task_id)
                result = _queue_get(queue, 10.0)
            finally:
                publisher.stop()
                manager.close()
                process.join(timeout=3.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3.0)
            passed = bool(result.get("ok")) and process.exitcode == 0
            return {
                "passed": passed,
                "child_exitcode": process.exitcode,
                "received_task_available": result.get("metrics", {}).get("received_task_available", 0),
                "received_task_events": result.get("metrics", {}).get("received_task_events", 0),
                "received_cancellations": result.get("metrics", {}).get("received_cancellations", 0),
                "cancel_signalled": result.get("cancel_signalled", False),
                "failure": result.get("error"),
            }

        cases.append(_run_case("redis_pubsub_cross_process", pubsub_case))

        def multi_worker_case() -> dict[str, Any]:
            store = MySQLTripTaskStore(engine)
            created_ids: list[str] = []
            for index in range(3):
                task, reused = store.create_task(
                    _request(index),
                    idempotency_key=f"{batch}-worker-{index}",
                )
                if reused:
                    raise AssertionError("多 Worker 验收任务被意外复用")
                created_ids.append(task.task_id)

            start = context.Event()
            ready = context.Queue()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_claim_one_process,
                    args=(mysql_config, f"acceptance-worker-{index}", start, ready, result_queue),
                    name=f"redis-acceptance-worker-{index}",
                )
                for index in range(3)
            ]
            for process in processes:
                process.start()
            ready_workers = {_queue_get(ready, 8.0) for _ in processes}
            start.set()
            results = [_queue_get(result_queue, 12.0) for _ in processes]
            for process in processes:
                process.join(timeout=3.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3.0)

            claimed_ids = [item.get("task_id") for item in results if item.get("ok")]
            worker_ids = [item.get("worker_id") for item in results if item.get("ok")]
            snapshots = [store.get_task(task_id) for task_id in created_ids]
            passed = (
                len(ready_workers) == 3
                and len(claimed_ids) == 3
                and len(set(claimed_ids)) == 3
                and set(claimed_ids) == set(created_ids)
                and len(set(worker_ids)) == 3
                and all(task.status == "succeeded" for task in snapshots)
                and all(process.exitcode == 0 for process in processes)
            )
            return {
                "passed": passed,
                "task_count": len(created_ids),
                "unique_claimed_tasks": len(set(claimed_ids)),
                "participating_workers": sorted(set(worker_ids)),
                "statuses": [task.status for task in snapshots],
                "worker_exitcodes": [process.exitcode for process in processes],
                "failures": [item.get("error") for item in results if not item.get("ok")],
            }

        cases.append(_run_case("mysql_multi_worker_concurrency", multi_worker_case))

        def failure_recovery_case() -> dict[str, Any]:
            executable = _find_redis_server(redis_server_path)
            port = _free_tcp_port()
            temporary_config = replace(
                redis_config,
                port=port,
                database=0,
                username=None,
                password=None,
                key_prefix=f"travel-agent:acceptance:fault:{uuid4().hex[:12]}",
            )
            server: subprocess.Popen | None = None
            subscriber_manager, subscriber = _redis_bus(temporary_config)
            publisher_manager, publisher = _redis_bus(temporary_config)
            try:
                server = _start_temporary_redis(executable, port)
                if not _wait_until(lambda: subscriber_manager.check_health().healthy is True, 5.0):
                    raise RuntimeError("临时 Redis 未能启动")
                subscriber.start()
                channels = (
                    subscriber.task_channel,
                    subscriber.event_channel,
                    subscriber.cancellation_channel,
                )
                if not _wait_until(lambda: _subscribers_ready(publisher_manager, channels), 5.0):
                    raise TimeoutError("故障验收订阅器未就绪")

                first_task = f"task-{uuid4().hex}"
                publisher.publish_task_available(first_task)
                if not _wait_until(
                    lambda: subscriber.metrics_snapshot().received_task_available >= 1,
                    3.0,
                ):
                    raise AssertionError("故障前通知未收到")

                _stop_temporary_redis(server)
                server = None
                if not _wait_until(lambda: subscriber.health_snapshot().degraded, 4.0):
                    raise AssertionError("Redis 停止后订阅器未进入降级状态")
                publisher.publish_task_available(f"offline-{uuid4().hex}")
                degraded_publish = publisher.metrics_snapshot().publish_degraded

                server = _start_temporary_redis(executable, port)
                if not _wait_until(lambda: publisher_manager.check_health().healthy is True, 5.0):
                    raise AssertionError("临时 Redis 重启后健康检查未恢复")
                if not _wait_until(lambda: _subscribers_ready(publisher_manager, channels), 8.0):
                    raise AssertionError("Redis 重启后 Pub/Sub 未重新订阅")

                before = subscriber.metrics_snapshot().received_task_available
                recovered_task = f"task-{uuid4().hex}"
                publisher.publish_task_available(recovered_task)
                received_after_recovery = _wait_until(
                    lambda: subscriber.metrics_snapshot().received_task_available > before,
                    4.0,
                )
                metrics = subscriber.metrics_snapshot()
                passed = (
                    degraded_publish >= 1
                    and received_after_recovery
                    and metrics.subscriber_reconnects >= 1
                    and not subscriber.health_snapshot().degraded
                )
                return {
                    "passed": passed,
                    "temporary_port": port,
                    "publish_degraded": degraded_publish,
                    "subscriber_reconnects": metrics.subscriber_reconnects,
                    "received_after_recovery": received_after_recovery,
                    "subscriber_degraded_after_recovery": subscriber.health_snapshot().degraded,
                }
            finally:
                subscriber.stop()
                publisher.stop()
                subscriber_manager.close()
                publisher_manager.close()
                _stop_temporary_redis(server)

        cases.append(_run_case("redis_failure_and_recovery", failure_recovery_case))

        def sse_cancel_case() -> dict[str, Any]:
            # 延迟导入 main，避免子进程导入时意外初始化 FastAPI 全局对象。
            import main
            from fastapi.testclient import TestClient

            delegate = MySQLTripTaskStore(engine)
            task, reused = delegate.create_task(
                _request(100),
                idempotency_key=f"{batch}-sse-cancel",
            )
            if reused:
                raise AssertionError("SSE 验收任务被意外复用")
            worker_id = "acceptance-sse-worker"
            claimed = delegate.claim_next(worker_id, lease_seconds=20.0)
            if claimed is None or claimed.task_id != task.task_id:
                raise AssertionError("SSE 验收任务未被预期 Worker 领取")
            initial_events = delegate.list_events(task.task_id)
            initial_cursor = max(event.event_id for event in initial_events)

            manager, bus = _redis_bus(redis_config)
            notifying_store = NotifyingTripTaskStore(delegate=delegate, notification_bus=bus)
            original_store = main.trip_task_store
            original_bus = main.task_notification_bus
            original_worker = main.trip_task_worker
            original_enabled = main.settings.TRIP_TASK_WORKER_ENABLED
            main.trip_task_store = notifying_store
            main.task_notification_bus = bus
            main.trip_task_worker = _DummyWorker()
            main.settings.TRIP_TASK_WORKER_ENABLED = False

            start = context.Event()
            result_queue = context.Queue()
            process = context.Process(
                target=_cancel_and_finish_process,
                args=(mysql_config, redis_config, task.task_id, worker_id, start, result_queue),
                name="redis-sse-cancel-acceptance",
            )
            event_ids: list[int] = []
            event_types: list[str] = []
            try:
                with TestClient(main.app) as client:
                    channels = (bus.task_channel, bus.event_channel, bus.cancellation_channel)
                    if not _wait_until(lambda: _subscribers_ready(manager, channels), 5.0):
                        raise TimeoutError("SSE 进程 Redis 订阅未就绪")
                    process.start()
                    # 先让独立进程提交取消和终态，再建立 HTTP 流。SSE 会从
                    # Last-Event-ID 之后回放数据库事件，因此即使 Pub/Sub 消息先到也不会丢事件。
                    start.set()
                    with client.stream(
                        "GET",
                        f"/api/trip/tasks/{task.task_id}/events",
                        headers={"Last-Event-ID": str(initial_cursor)},
                    ) as response:
                        if response.status_code != 200:
                            raise AssertionError(f"SSE 返回状态异常: {response.status_code}")
                        current_event_id: int | None = None
                        for line in response.iter_lines():
                            if line.startswith("id: "):
                                current_event_id = int(line[4:])
                                event_ids.append(current_event_id)
                            elif line.startswith("event: "):
                                event_types.append(line[7:])
                            if event_types and event_types[-1] == "task_cancelled":
                                break
                    child_result = _queue_get(result_queue, 8.0)
                process.join(timeout=3.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3.0)
                final_task = delegate.get_task(task.task_id)
                database_events = delegate.list_events(task.task_id, after_event_id=initial_cursor)
                database_ids = [event.event_id for event in database_events]
                cancellation_received = bus.is_cancel_signalled(task.task_id)
                metrics = bus.metrics_snapshot()
                passed = (
                    child_result.get("ok") is True
                    and final_task.status == "cancelled"
                    and cancellation_received
                    and "cancellation_requested" in event_types
                    and "task_cancelled" in event_types
                    and event_ids == database_ids
                    and len(event_ids) == len(set(event_ids))
                    and process.exitcode == 0
                    and metrics.received_cancellations >= 1
                )
                return {
                    "passed": passed,
                    "http_status": 200,
                    "event_ids": event_ids,
                    "event_types": event_types,
                    "database_event_ids": database_ids,
                    "unique_event_ids": len(set(event_ids)),
                    "cancel_signalled_cross_process": cancellation_received,
                    "received_cancellations": metrics.received_cancellations,
                    "final_status": final_task.status,
                    "child_exitcode": process.exitcode,
                    "child_error": child_result.get("error"),
                }
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3.0)
                bus.stop()
                manager.close()
                main.trip_task_store = original_store
                main.task_notification_bus = original_bus
                main.trip_task_worker = original_worker
                main.settings.TRIP_TASK_WORKER_ENABLED = original_enabled

        cases.append(_run_case("sse_and_cancellation_cross_process", sse_cancel_case))
    finally:
        _cleanup_acceptance_tasks(engine, batch)
        engine.dispose()

    return RedisRuntimeAcceptanceReport(
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        database=database,
        redis_target=base_redis_config.safe_target(),
        cases=tuple(cases),
    )


def write_redis_runtime_report(report: RedisRuntimeAcceptanceReport, path: str | Path) -> Path:
    """写入脱敏 JSON 报告，供本地质量门和 CI 保存。"""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output
