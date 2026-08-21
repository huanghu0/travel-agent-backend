"""运行 Redis 并发压力测试，并输出连接池和轮询间隔建议。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.evaluation.redis_load_test import run_redis_load_test, write_redis_load_report
from app.infrastructure.redis import RedisConfig


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Redis 并发压力和调优验收，不调用业务 Provider。")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--operations-per-worker", type=int, default=50)
    parser.add_argument("--notification-count", type=int, default=200)
    parser.add_argument("--max-p95-latency-ms", type=float, default=100.0)
    parser.add_argument("--max-notification-p95-latency-ms", type=float, default=200.0)
    parser.add_argument("--minimum-notification-ratio", type=float, default=0.99)
    parser.add_argument("--worker-processes", type=int, default=2)
    parser.add_argument("--expected-sse-connections", type=int, default=50)
    parser.add_argument(
        "--json-report",
        type=Path,
        default=Path("build/reports/redis-load-test.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = _args()
    try:
        report = run_redis_load_test(
            redis_config=RedisConfig.from_settings(settings),
            concurrency=args.concurrency,
            operations_per_worker=args.operations_per_worker,
            notification_count=args.notification_count,
            max_p95_latency_ms=args.max_p95_latency_ms,
            max_notification_p95_latency_ms=(
                args.max_notification_p95_latency_ms
            ),
            minimum_notification_receive_ratio=args.minimum_notification_ratio,
            worker_processes=args.worker_processes,
            expected_sse_connections=args.expected_sse_connections,
        )
    except Exception as exc:
        # 即使 Redis 完全离线，也输出可被 CI/运维平台消费的结构化失败文件。
        failure = {
            "passed": False,
            "stage": "redis_load_test",
            "error_type": exc.__class__.__name__,
            "error": " ".join(str(exc).split())[:500],
        }
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        print(f"JSON report: {args.json_report.resolve()}")
        return 2

    output = write_redis_load_report(report, args.json_report)
    print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    print(f"JSON report: {output.resolve()}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
