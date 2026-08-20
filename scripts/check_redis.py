"""从命令行检查本地 Redis 连接，不输出用户名或密码。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 支持从项目根目录直接运行脚本，而不要求调用方额外设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.infrastructure.redis import RedisClientManager, RedisConfig, RedisHealthStatus


def main() -> int:
    parser = argparse.ArgumentParser(description="检查旅行智能体 Redis 基础设施")
    parser.add_argument(
        "--require-enabled",
        action="store_true",
        help="REDIS_ENABLED=false 时也返回非零退出码",
    )
    args = parser.parse_args()

    manager = RedisClientManager(RedisConfig.from_settings(settings))
    try:
        health = manager.check_health()
    finally:
        manager.close()

    print(json.dumps(health.model_dump(), ensure_ascii=False))
    if health.status == RedisHealthStatus.OK:
        return 0
    if health.status == RedisHealthStatus.DISABLED and not args.require_enabled:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

