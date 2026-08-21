"""运行 Redis 跨进程、并发 Worker、故障恢复和 SSE/取消验收。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.evaluation.redis_runtime_acceptance import (
    run_redis_runtime_acceptance,
    write_redis_runtime_report,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行真实 Redis/MySQL 任务运行时验收，不调用高德或 LLM。"
    )
    parser.add_argument(
        "--database",
        default=settings.MYSQL_TEST_DATABASE,
        help="独立 MySQL 测试库，默认 MYSQL_TEST_DATABASE；拒绝使用 MYSQL_DATABASE。",
    )
    parser.add_argument(
        "--redis-server",
        default=None,
        help="redis-server 可执行文件路径；故障恢复验收会启动独立临时实例。",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        default=Path("build/reports/redis-runtime-acceptance.json"),
        help="脱敏 JSON 报告输出路径。",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = run_redis_runtime_acceptance(
        mysql_database=args.database,
        redis_server_path=args.redis_server,
    )
    output = write_redis_runtime_report(report, args.json_report)

    print("case | result | duration_ms | details")
    print("-" * 110)
    for case in report.cases:
        details = json.dumps(case.details, ensure_ascii=False, separators=(",", ":"))
        print(
            f"{case.name} | {'PASS' if case.passed else 'FAIL'} | "
            f"{case.duration_ms:.3f} | {details}"
        )
        if case.error:
            print(f"  error: {case.error}")

    print()
    print(
        "Redis runtime acceptance: "
        f"{sum(case.passed for case in report.cases)}/{len(report.cases)} passed"
    )
    print(f"JSON report: {output.resolve()}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
