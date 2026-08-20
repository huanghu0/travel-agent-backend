"""从命令行检查本地 Redis 连接和通用缓存读写，不输出用户名或密码。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

# 支持从项目根目录直接运行脚本，而不要求调用方额外设置 PYTHONPATH。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.infrastructure.cache import (
    CacheConfig,
    CacheReadStatus,
    create_cache_store,
)
from app.infrastructure.redis import RedisClientManager, RedisConfig, RedisHealthStatus


def _run_cache_smoke_test(manager: RedisClientManager) -> tuple[dict, bool]:
    """写入短 TTL 测试值并立即清理，验证完整 CacheStore 链路。"""

    cache_store = create_cache_store(
        cache_config=CacheConfig.from_settings(settings),
        redis_client_manager=manager,
    )
    if not cache_store.enabled:
        return {
            "status": "skipped",
            "reason": "redis_disabled",
            "backend": cache_store.backend_name,
        }, False

    key = manager.key_builder.literal("cache", "smoke", uuid4().hex)
    cleanup_succeeded = False
    try:
        write = cache_store.set(
            key,
            {"check": "redis-cache", "schema_version": cache_store.schema_version},
            ttl_seconds=30,
        )
        lookup = cache_store.get(key)
        remaining_ttl = manager.execute(
            lambda client: client.ttl(key),
            fallback=None,
        )
    finally:
        # 唯一测试 Key 无论成功或失败都尝试删除，避免污染开发 Redis。
        cleanup_succeeded = cache_store.delete(key)

    success = (
        write.stored
        and lookup.status == CacheReadStatus.HIT
        and isinstance(remaining_ttl, int)
        and remaining_ttl > 0
        and cleanup_succeeded
    )
    return {
        "status": "ok" if success else "failed",
        "backend": cache_store.backend_name,
        "schema_version": cache_store.schema_version,
        "write_status": write.status.value,
        "read_status": lookup.status.value,
        "remaining_ttl_seconds": remaining_ttl,
        "cleanup_succeeded": cleanup_succeeded,
        "metrics": cache_store.metrics_snapshot().model_dump(),
    }, success


def main() -> int:
    parser = argparse.ArgumentParser(description="检查旅行智能体 Redis 基础设施")
    parser.add_argument(
        "--require-enabled",
        action="store_true",
        help="REDIS_ENABLED=false 时也返回非零退出码",
    )
    parser.add_argument(
        "--cache-smoke-test",
        action="store_true",
        help="执行一次版本化 JSON 缓存 set/get/TTL/delete 冒烟测试",
    )
    args = parser.parse_args()

    manager = RedisClientManager(RedisConfig.from_settings(settings))
    cache_success = True
    try:
        health = manager.check_health()
        if args.cache_smoke_test:
            cache_report, cache_success = _run_cache_smoke_test(manager)
            report = {
                "redis": health.model_dump(),
                "cache_smoke": cache_report,
            }
        else:
            report = health.model_dump()
    finally:
        manager.close()

    print(json.dumps(report, ensure_ascii=False))
    if health.status == RedisHealthStatus.OK and cache_success:
        return 0
    if (
        health.status == RedisHealthStatus.DISABLED
        and not args.require_enabled
        and not args.cache_smoke_test
    ):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
