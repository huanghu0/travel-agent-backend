import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import Mock

from app.core.llm import ResponsesLLM
from app.infrastructure.cache.models import (
    CacheLookup,
    CacheReadStatus,
    CacheWriteResult,
    CacheWriteStatus,
)
from app.infrastructure.cache.read_models import ReadModelSnapshotCache
from app.infrastructure.redis.keys import RedisKeyBuilder
from app.infrastructure.redis.rate_limit import (
    ProviderQuotaController,
    ProviderQuotaExceededError,
    QuotaPolicy,
    RedisRateLimiter,
)
from app.providers.amap.business_cache import AmapBusinessCache
from app.providers.amap.client import AmapProviderClient
from app.task_runtime.models import TripPlanningTask
from app.schemas.trip_schema import TripRequest


class MemoryCacheStore:
    backend_name = "memory"
    enabled = True
    schema_version = 1

    def __init__(self):
        self.values = {}
        self.get_calls = 0
        self.set_calls = 0

    def get(self, key):
        self.get_calls += 1
        if key not in self.values:
            return CacheLookup(CacheReadStatus.MISS)
        return CacheLookup(CacheReadStatus.HIT, self.values[key])

    def set(self, key, value, *, ttl_seconds=None):
        self.set_calls += 1
        self.values[key] = value
        return CacheWriteResult(CacheWriteStatus.STORED, ttl_seconds=ttl_seconds)

    def delete(self, key):
        return self.values.pop(key, None) is not None

    def metrics_snapshot(self):
        return SimpleNamespace(model_dump=lambda: {})


class SharedFakeRedis:
    """只实现限流 Lua 测试需要的 eval，多个 manager 共享同一计数。"""

    def __init__(self):
        self.counts = defaultdict(int)
        self.ttl_ms = {}

    def eval(self, script, numkeys, key, cost, window_ms):
        del script, numkeys
        self.counts[key] += int(cost)
        self.ttl_ms.setdefault(key, int(window_ms))
        return [self.counts[key], self.ttl_ms[key]]


class FakeRedisManager:
    def __init__(self, client=None, *, degraded=False):
        self.client = client
        self.degraded = degraded

    def execute(self, operation, *, fallback=None):
        if self.degraded:
            return fallback
        return operation(self.client)


class RedisRateLimitTests(unittest.TestCase):
    def test_two_instances_share_the_same_quota_counter(self):
        redis = SharedFakeRedis()
        policy = QuotaPolicy("per-minute", 2, 60)
        builder = RedisKeyBuilder("travel-agent:test")
        first = RedisRateLimiter(FakeRedisManager(redis), key_builder=builder)
        second = RedisRateLimiter(FakeRedisManager(redis), key_builder=builder)

        self.assertTrue(first.acquire(provider="amap", policy=policy).allowed)
        self.assertTrue(second.acquire(provider="amap", policy=policy).allowed)
        denied = first.acquire(provider="amap", policy=policy)

        self.assertFalse(denied.allowed)
        self.assertEqual(0, denied.remaining)
        self.assertGreater(denied.retry_after_seconds, 0)

    def test_redis_failure_is_fail_open_and_observable(self):
        limiter = RedisRateLimiter(
            FakeRedisManager(degraded=True),
            key_builder=RedisKeyBuilder("travel-agent:test"),
            fail_open=True,
        )

        decision = limiter.acquire(
            provider="llm",
            policy=QuotaPolicy("per-minute", 1, 60),
            identity="model-a",
        )

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.degraded)
        self.assertEqual(1, limiter.metrics_snapshot().degraded_allowed)

    def test_llm_request_is_rejected_before_network_call(self):
        redis = SharedFakeRedis()
        limiter = RedisRateLimiter(
            FakeRedisManager(redis), key_builder=RedisKeyBuilder("travel-agent:test")
        )
        controller = ProviderQuotaController(
            limiter,
            llm_policies=(QuotaPolicy("per-minute", 1, 60),),
        )
        responses = SimpleNamespace(create=Mock(return_value=SimpleNamespace(output_text="ok")))
        client = SimpleNamespace(responses=responses)
        llm = ResponsesLLM(client=client, model="model-a", quota_controller=controller)

        self.assertEqual("ok", llm.invoke("system", "first"))
        with self.assertRaises(ProviderQuotaExceededError):
            llm.invoke("system", "second")
        self.assertEqual(1, responses.create.call_count)


class AmapBusinessCacheTests(unittest.TestCase):
    def test_weather_uses_redis_standardized_result_cache(self):
        class RawClient:
            calls = 0

            @classmethod
            def get_weather(cls, city):
                cls.calls += 1
                return {
                    "status": "1",
                    "forecasts": [{
                        "city": city,
                        "province": "浙江",
                        "casts": [{
                            "date": "2026-08-22",
                            "dayweather": "晴",
                            "nightweather": "多云",
                            "daytemp": "32",
                            "nighttemp": "25",
                        }],
                    }],
                }

        store = MemoryCacheStore()
        business_cache = AmapBusinessCache(
            store, RedisKeyBuilder("travel-agent:test")
        )
        provider = AmapProviderClient(
            raw_client=RawClient,
            business_cache=business_cache,
        )

        first = provider.get_weather("杭州")
        second = provider.get_weather("杭州")

        self.assertEqual(first, second)
        self.assertEqual(1, RawClient.calls)
        metrics = business_cache.metrics_snapshot().domains["weather"]
        self.assertEqual(1, metrics["hits"])
        self.assertEqual(1, metrics["provider_calls"])


class ReadModelSnapshotCacheTests(unittest.TestCase):
    def test_task_progress_snapshot_round_trip(self):
        store = MemoryCacheStore()
        cache = ReadModelSnapshotCache(
            store,
            RedisKeyBuilder("travel-agent:test"),
            execution_view_ttl_seconds=1800,
            task_active_ttl_seconds=3600,
            task_terminal_ttl_seconds=86400,
        )
        request = TripRequest(
            city="杭州",
            start_date="2026-08-22",
            end_date="2026-08-22",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            preferences=["自然风光"],
            free_text_input="",
        )
        task = TripPlanningTask(
            task_id="task-1",
            session_id="session-1",
            idempotency_key="idem-1",
            request_fingerprint="fingerprint",
            request=request,
        )

        cache.set_task_progress(task.task_id, task, terminal=False)
        loaded = cache.get_task_progress(task.task_id, TripPlanningTask)

        self.assertIsNotNone(loaded)
        self.assertEqual(task.task_id, loaded.task_id)
        self.assertEqual("杭州", loaded.request.city)


if __name__ == "__main__":
    unittest.main()
