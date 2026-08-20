import unittest

from app.infrastructure.cache.models import (
    CacheLookup,
    CacheReadStatus,
    CacheWriteResult,
    CacheWriteStatus,
)
from app.persistence import CacheStoreEntry
from app.providers.amap.client import AmapProviderClient
from app.providers.amap.layered_cache import (
    LayeredCacheSource,
    LayeredRestaurantCache,
    LayeredRouteCache,
)
from app.providers.amap.restaurant_cache import restaurant_search_cache_key
from app.providers.amap.route_cache import route_leg_cache_key
from tests.test_restaurant_cache import make_anchor, make_snapshot
from tests.test_route_cache import make_estimate, make_leg


class MemoryL1:
    """测试用通用缓存，模拟 Redis 命中、降级和写入。"""

    backend_name = "memory"
    enabled = True
    schema_version = 1

    def __init__(self):
        self.values = {}
        self.read_status = None
        self.set_calls = []
        self.delete_calls = []

    def get(self, key):
        if self.read_status is not None:
            return CacheLookup(status=self.read_status)
        if key not in self.values:
            return CacheLookup(status=CacheReadStatus.MISS)
        return CacheLookup(status=CacheReadStatus.HIT, value=self.values[key])

    def set(self, key, value, *, ttl_seconds=None):
        self.values[key] = value
        self.set_calls.append((key, ttl_seconds))
        return CacheWriteResult(
            status=CacheWriteStatus.STORED,
            ttl_seconds=ttl_seconds,
        )

    def delete(self, key):
        self.delete_calls.append(key)
        return self.values.pop(key, None) is not None


class MemoryL2:
    """测试用数据库 L2，显式返回剩余 TTL。"""

    def __init__(self):
        self.entries = {}
        self.get_calls = 0
        self.set_calls = []
        self.raise_on_get = False
        self.raise_on_set = False

    def get_entry(self, key):
        self.get_calls += 1
        if self.raise_on_get:
            raise RuntimeError("l2 read failed")
        return self.entries.get(key)

    def get(self, key):
        entry = self.get_entry(key)
        return entry.value if entry else None

    def set(self, key, value, *, ttl_seconds):
        self.set_calls.append((key, ttl_seconds))
        if self.raise_on_set:
            raise RuntimeError("l2 write failed")
        self.entries[key] = CacheStoreEntry(value=value, remaining_ttl_seconds=ttl_seconds)

    def purge_expired(self):
        return 0


def route_cache(l1, l2):
    return LayeredRouteCache(
        l1_cache=l1,
        l2_cache=l2,
        l1_key_builder=lambda key: f"route:{key}",
    )


def restaurant_cache(l1, l2):
    return LayeredRestaurantCache(
        l1_cache=l1,
        l2_cache=l2,
        l1_key_builder=lambda key: f"restaurant:{key}",
    )


class LayeredCacheTests(unittest.TestCase):
    def test_l1_hit_skips_l2_and_counts_avoided_provider_call(self):
        l1, l2 = MemoryL1(), MemoryL2()
        cache = route_cache(l1, l2)
        l1.values["route:key"] = make_estimate().model_dump(mode="json")

        lookup = cache.lookup("key")

        self.assertEqual(lookup.source, LayeredCacheSource.L1)
        self.assertEqual(l2.get_calls, 0)
        metrics = cache.metrics_snapshot()
        self.assertEqual(metrics.l1_hits, 1)
        self.assertEqual(metrics.provider_calls_avoided_by_l1, 1)
        self.assertEqual(metrics.l1_hit_rate, 1.0)

    def test_l2_hit_backfills_l1_with_remaining_ttl(self):
        l1, l2 = MemoryL1(), MemoryL2()
        l2.entries["key"] = CacheStoreEntry(
            value=make_estimate(),
            remaining_ttl_seconds=37,
        )
        cache = route_cache(l1, l2)

        lookup = cache.lookup("key")

        self.assertEqual(lookup.source, LayeredCacheSource.L2)
        self.assertEqual(l1.set_calls, [("route:key", 37)])
        metrics = cache.metrics_snapshot()
        self.assertEqual(metrics.l2_hits, 1)
        self.assertEqual(metrics.l1_backfills, 1)
        self.assertEqual(metrics.provider_calls_avoided_by_l2, 1)

    def test_invalid_l1_payload_is_deleted_and_falls_back_to_l2(self):
        l1, l2 = MemoryL1(), MemoryL2()
        l1.values["route:key"] = {"unexpected": True}
        l2.entries["key"] = CacheStoreEntry(make_estimate(), 20)
        cache = route_cache(l1, l2)

        lookup = cache.lookup("key")

        self.assertEqual(lookup.source, LayeredCacheSource.L2)
        self.assertEqual(l1.delete_calls, ["route:key"])
        self.assertEqual(cache.metrics_snapshot().l1_invalid_payloads, 1)

    def test_redis_degraded_still_uses_l2(self):
        l1, l2 = MemoryL1(), MemoryL2()
        l1.read_status = CacheReadStatus.DEGRADED
        l2.entries["key"] = CacheStoreEntry(make_estimate(), 15)
        cache = route_cache(l1, l2)

        lookup = cache.lookup("key")

        self.assertEqual(lookup.source, LayeredCacheSource.L2)
        self.assertEqual(cache.metrics_snapshot().l1_degraded, 1)

    def test_l2_write_failure_still_populates_l1(self):
        l1, l2 = MemoryL1(), MemoryL2()
        l2.raise_on_set = True
        cache = route_cache(l1, l2)

        with self.assertLogs("app.providers.amap.layered_cache", level="WARNING"):
            cache.set("key", make_estimate(), ttl_seconds=60)

        self.assertIn("route:key", l1.values)
        self.assertEqual(cache.metrics_snapshot().l2_write_errors, 1)

    def test_l2_read_failure_becomes_provider_miss(self):
        l1, l2 = MemoryL1(), MemoryL2()
        l2.raise_on_get = True
        with self.assertLogs("app.providers.amap.layered_cache", level="WARNING"):
            lookup = route_cache(l1, l2).lookup("key")

        self.assertEqual(lookup.source, LayeredCacheSource.MISS)
        self.assertEqual(lookup.l2_status, "error")


class LayeredProviderTests(unittest.TestCase):
    def test_route_uses_provider_once_then_redis_l1(self):
        l1, l2 = MemoryL1(), MemoryL2()
        cache = route_cache(l1, l2)

        class RawClient:
            calls = 0

            @classmethod
            def route(cls, **kwargs):
                cls.calls += 1
                return {
                    "status": "1",
                    "info": "OK",
                    "route": {
                        "paths": [{"distance": "1200", "cost": {"duration": "900"}}]
                    },
                }

        provider = AmapProviderClient(raw_client=RawClient, route_cache=cache)
        leg = make_leg()
        first = provider.estimate_routes(
            city="成都", plan_fingerprint="first", legs=[leg]
        )
        second = provider.estimate_routes(
            city="成都", plan_fingerprint="second", legs=[leg]
        )

        self.assertEqual(RawClient.calls, 1)
        self.assertEqual(first.provider_calls, 1)
        self.assertEqual(first.l1_cache_misses, 1)
        self.assertEqual(first.l2_cache_misses, 1)
        self.assertEqual(second.l1_cache_hits, 1)
        self.assertEqual(second.provider_calls_avoided_by_l1, 1)
        self.assertEqual(cache.metrics_snapshot().provider_calls, 1)

    def test_restaurant_l2_hit_skips_provider_and_reports_layer(self):
        l1, l2 = MemoryL1(), MemoryL2()
        cache = restaurant_cache(l1, l2)
        anchor = make_anchor()
        key = restaurant_search_cache_key(
            city="杭州",
            keywords="餐厅",
            center=anchor.location,
            radius_meters=2500,
            page_size=4,
        )
        # 测试显式参数与生产默认值解耦，确保 Key 完全一致。
        l2.entries[key] = CacheStoreEntry(make_snapshot(), 45)

        class RawClient:
            @classmethod
            def around_search(cls, **kwargs):
                raise AssertionError("L2 命中后不应调用高德")

        result = AmapProviderClient(
            raw_client=RawClient, restaurant_cache=cache
        ).search_restaurants(
            city="杭州",
            anchors=[anchor],
            radius_meters=2500,
            candidates_per_anchor=4,
        )

        self.assertEqual(result.l2_cache_hits, 1)
        self.assertEqual(result.provider_calls, 0)
        self.assertEqual(result.provider_calls_avoided_by_l2, 1)
        self.assertEqual(result.candidates[0].poi_id, "food-1")


if __name__ == "__main__":
    unittest.main()
