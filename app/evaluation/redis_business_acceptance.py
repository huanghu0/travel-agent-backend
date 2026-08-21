"""Redis 供应商限流、配额和故障恢复的真实业务验收。

验收只访问独立 Redis Key，并使用本地临时 Redis 进程模拟故障与恢复；
不会调用高德、LLM，也不会读写旅行规划事实数据。
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty
from typing import Any, Callable
from uuid import uuid4

from app.core.config import settings
from app.infrastructure.redis import RedisClientManager, RedisConfig
from app.infrastructure.redis.rate_limit import QuotaPolicy, RedisRateLimiter


@dataclass(frozen=True, slots=True)
class RedisBusinessAcceptanceCase:
    """单项业务验收结果，details 不包含 Key、密钥或用户输入。"""

    name: str
    passed: bool
    duration_ms: float
    details: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RedisBusinessAcceptanceReport:
    """Redis 第三阶段业务增强验收报告。"""

    started_at: str
    redis_target: str
    cases: tuple[RedisBusinessAcceptanceCase, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def model_dump(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        payload["passed_cases"] = sum(case.passed for case in self.cases)
        payload["total_cases"] = len(self.cases)
        return payload


def _run_case(name: str, callback: Callable[[], dict[str, Any]]) -> RedisBusinessAcceptanceCase:
    started = time.perf_counter()
    try:
        details = callback()
        passed = bool(details.pop("passed", False))
        return RedisBusinessAcceptanceCase(
            name=name,
            passed=passed,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            details=details,
            error=None if passed else "验收断言未通过",
        )
    except Exception as exc:
        return RedisBusinessAcceptanceCase(
            name=name,
            passed=False,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            details={},
            error=f"{exc.__class__.__name__}: {exc}",
        )


def _wait_until(predicate: Callable[[], bool], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def _queue_get(queue, timeout_seconds: float) -> Any:
    try:
        return queue.get(timeout=timeout_seconds)
    except Empty as exc:
        raise TimeoutError("等待跨进程限流结果超时") from exc


def _quota_worker(
    config: RedisConfig,
    policy: QuotaPolicy,
    attempts: int,
    start,
    result_queue,
) -> None:
    """独立进程使用独立连接池，模拟不同 API/Worker 实例。"""

    manager = RedisClientManager(config)
    limiter = RedisRateLimiter(manager, key_builder=manager.key_builder, fail_open=False)
    try:
        if not start.wait(8.0):
            raise TimeoutError("等待并发限流验收开始超时")
        allowed = 0
        rejected = 0
        degraded = 0
        for _ in range(attempts):
            decision = limiter.acquire(provider="amap", policy=policy)
            allowed += int(decision.allowed)
            rejected += int(not decision.allowed)
            degraded += int(decision.degraded)
        result_queue.put(
            {
                "ok": True,
                "allowed": allowed,
                "rejected": rejected,
                "degraded": degraded,
            }
        )
    except Exception as exc:
        result_queue.put({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
    finally:
        manager.close()


def _delete_prefix(manager: RedisClientManager, prefix: str) -> int:
    """只清理本次唯一验收前缀，绝不执行 FLUSHDB。"""

    def operation(client) -> int:
        deleted = 0
        for key in client.scan_iter(match=f"{prefix}:*", count=100):
            deleted += int(client.delete(key))
        return deleted

    return int(manager.execute(operation, fallback=0) or 0)


def _find_redis_server(explicit_path: str | None = None) -> str:
    candidates = [explicit_path, shutil.which("redis-server")]
    if os.name == "nt":
        candidates.extend([r"D:\redis\redis-server.exe", r"C:\redis\redis-server.exe"])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("未找到 redis-server；无法执行限流故障/恢复验收")


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_temporary_redis(executable: str, port: int) -> subprocess.Popen:
    command = [
        executable,
        "--bind", "127.0.0.1",
        "--port", str(port),
        "--save", "",
        "--appendonly", "no",
    ]
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
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


def run_redis_business_acceptance(
    *,
    redis_server_path: str | None = None,
) -> RedisBusinessAcceptanceReport:
    """执行真实 Redis 跨进程额度协调和 fail-open 恢复验收。"""

    base_config = RedisConfig.from_settings(settings)
    if not base_config.enabled:
        raise RuntimeError("REDIS_ENABLED=false，无法执行 Redis 业务验收")

    unique_prefix = f"travel-agent:acceptance:{uuid4().hex}"
    shared_config = replace(base_config, key_prefix=unique_prefix)
    cleanup_manager = RedisClientManager(shared_config)
    cases: list[RedisBusinessAcceptanceCase] = []

    try:
        def health_case() -> dict[str, Any]:
            health = cleanup_manager.check_health()
            return {
                "passed": health.healthy is True,
                "healthy": health.healthy,
                "degraded": health.degraded,
                "latency_ms": health.latency_ms,
            }

        cases.append(_run_case("configured_redis_health", health_case))

        def cross_process_quota_case() -> dict[str, Any]:
            policy = QuotaPolicy("acceptance-window", limit=12, window_seconds=30)
            process_count = 3
            attempts_per_process = 10
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_quota_worker,
                    args=(shared_config, policy, attempts_per_process, start, result_queue),
                )
                for _ in range(process_count)
            ]
            for process in processes:
                process.start()
            start.set()
            results = [_queue_get(result_queue, 15.0) for _ in processes]
            for process in processes:
                process.join(timeout=5.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3.0)
            if not all(item.get("ok") for item in results):
                raise AssertionError(f"子进程失败: {results}")
            allowed = sum(int(item["allowed"]) for item in results)
            rejected = sum(int(item["rejected"]) for item in results)
            degraded = sum(int(item["degraded"]) for item in results)
            total = process_count * attempts_per_process
            return {
                "passed": allowed == policy.limit and rejected == total - policy.limit and degraded == 0,
                "processes": process_count,
                "attempts": total,
                "limit": policy.limit,
                "allowed": allowed,
                "rejected": rejected,
                "degraded": degraded,
            }

        cases.append(_run_case("cross_process_provider_quota", cross_process_quota_case))

        def concurrent_connections_case() -> dict[str, Any]:
            # 同一进程内再使用两个独立连接池进行高并发，覆盖连接池并发路径。
            prefix = f"{unique_prefix}:threads"
            config = replace(shared_config, key_prefix=prefix)
            first = RedisClientManager(config)
            second = RedisClientManager(config)
            policy = QuotaPolicy("thread-window", limit=20, window_seconds=30)
            first_limiter = RedisRateLimiter(first, key_builder=first.key_builder, fail_open=False)
            second_limiter = RedisRateLimiter(second, key_builder=second.key_builder, fail_open=False)
            try:
                def acquire(index: int) -> bool:
                    limiter = first_limiter if index % 2 == 0 else second_limiter
                    return limiter.acquire(provider="llm", policy=policy, identity="acceptance-model").allowed

                with ThreadPoolExecutor(max_workers=16) as executor:
                    results = list(executor.map(acquire, range(50)))
                allowed = sum(results)
                return {
                    "passed": allowed == policy.limit,
                    "attempts": len(results),
                    "limit": policy.limit,
                    "allowed": allowed,
                    "rejected": len(results) - allowed,
                }
            finally:
                first.close()
                second.close()

        cases.append(_run_case("concurrent_connection_pools", concurrent_connections_case))

        def fail_open_recovery_case() -> dict[str, Any]:
            executable = _find_redis_server(redis_server_path)
            port = _free_tcp_port()
            temp_prefix = f"travel-agent:acceptance:recovery:{uuid4().hex}"
            config = replace(
                base_config,
                host="127.0.0.1",
                port=port,
                database=0,
                username=None,
                password=None,
                ssl=False,
                key_prefix=temp_prefix,
                socket_connect_timeout_seconds=0.25,
                socket_timeout_seconds=0.25,
                degrade_cooldown_seconds=0.1,
            )
            manager = RedisClientManager(config)
            limiter = RedisRateLimiter(manager, key_builder=manager.key_builder, fail_open=True)
            policy = QuotaPolicy("recovery-window", limit=5, window_seconds=30)
            server: subprocess.Popen | None = None
            try:
                server = _start_temporary_redis(executable, port)
                if not _wait_until(lambda: manager.check_health().healthy is True, 5.0):
                    raise AssertionError("临时 Redis 未启动")
                initial = limiter.acquire(provider="amap", policy=policy)

                _stop_temporary_redis(server)
                server = None
                if not _wait_until(lambda: manager.check_health().healthy is False, 3.0):
                    raise AssertionError("Redis 停止后未进入降级状态")
                degraded = limiter.acquire(provider="amap", policy=policy)

                server = _start_temporary_redis(executable, port)
                recovered = _wait_until(lambda: manager.check_health().healthy is True, 5.0)
                after_recovery = limiter.acquire(provider="amap", policy=policy)
                metrics = limiter.metrics_snapshot()
                client_metrics = manager.metrics_snapshot()
                return {
                    "passed": (
                        initial.allowed
                        and not initial.degraded
                        and degraded.allowed
                        and degraded.degraded
                        and recovered
                        and after_recovery.allowed
                        and not after_recovery.degraded
                        and metrics.degraded_allowed >= 1
                        and client_metrics.recoveries >= 1
                    ),
                    "fail_open_allowed": degraded.allowed,
                    "degraded_decisions": metrics.degraded_allowed,
                    "recovered": recovered,
                    "client_recoveries": client_metrics.recoveries,
                }
            finally:
                manager.close()
                _stop_temporary_redis(server)

        cases.append(_run_case("rate_limit_failure_and_recovery", fail_open_recovery_case))
    finally:
        _delete_prefix(cleanup_manager, unique_prefix)
        cleanup_manager.close()

    return RedisBusinessAcceptanceReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        redis_target=base_config.safe_target(),
        cases=tuple(cases),
    )


def write_redis_business_report(
    report: RedisBusinessAcceptanceReport,
    path: str | Path,
) -> Path:
    """写入脱敏 JSON 验收报告。"""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output
