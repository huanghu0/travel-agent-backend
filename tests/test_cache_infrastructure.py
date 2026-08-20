"""通用缓存抽象、版本化 JSON、TTL、降级和指标测试。"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

from app.infrastructure.cache import (
    CacheConfig,
    CacheEnvelopeSerializer,
    CacheReadStatus,
    CacheSchemaVersionError,
    CacheSerializationError,
    CacheStore,
    CacheTTLPolicy,
    CacheWriteStatus,
    NoOpCacheStore,
    create_cache_store,
)
from app.infrastructure.redis import RedisCacheStore, RedisClientManager, RedisConfig


class FakePool:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class FakeCacheRedis:
    """只实现缓存测试需要的 Redis 命令，不访问网络。"""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.get_error: Exception | None = None
        self.set_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.set_result: bool = True
        self.close_calls = 0
        self.connection_pool = FakePool()

    def get(self, key: str):
        if self.get_error is not None:
            raise self.get_error
        return self.data.get(key)

    def set(self, key: str, value: bytes, *, ex: int):
        if self.set_error is not None:
            raise self.set_error
        if self.set_result:
            self.data[key] = value
            self.ttls[key] = ex
        return self.set_result

    def delete(self, key: str) -> int:
        if self.delete_error is not None:
            raise self.delete_error
        existed = key in self.data
        self.data.pop(key, None)
        self.ttls.pop(key, None)
        return int(existed)

    def close(self) -> None:
        self.close_calls += 1


class Color(str, Enum):
    RED = "red"


@dataclass
class ExampleDataclass:
    name: str
    created_at: datetime


class ExampleModel(BaseModel):
    city: str
    price: Decimal


class CacheConfigTests(unittest.TestCase):
    def test_from_settings_reads_cache_policy(self):
        source = SimpleNamespace(
            REDIS_ENABLED=True,
            REDIS_CACHE_SCHEMA_VERSION=2,
            REDIS_DEFAULT_TTL_SECONDS=600,
            REDIS_CACHE_MIN_TTL_SECONDS=5,
            REDIS_CACHE_MAX_TTL_SECONDS=3600,
            REDIS_CACHE_DELETE_INVALID_ENTRIES=False,
        )

        config = CacheConfig.from_settings(source)

        self.assertTrue(config.enabled)
        self.assertEqual(config.schema_version, 2)
        self.assertEqual(config.default_ttl_seconds, 600)
        self.assertEqual(config.min_ttl_seconds, 5)
        self.assertEqual(config.max_ttl_seconds, 3600)
        self.assertFalse(config.delete_invalid_entries)

    def test_invalid_version_and_ttl_boundaries_are_rejected(self):
        with self.assertRaises(ValueError):
            CacheConfig(schema_version=0)
        with self.assertRaises(ValueError):
            CacheConfig(min_ttl_seconds=10, max_ttl_seconds=5)
        with self.assertRaises(ValueError):
            CacheConfig(default_ttl_seconds=0)
        with self.assertRaises(TypeError):
            CacheConfig(schema_version=True)


class CacheSerializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc)
        self.serializer = CacheEnvelopeSerializer(1, clock=lambda: self.now)

    def test_round_trip_uses_utf8_stable_json_envelope(self):
        first = self.serializer.dumps(
            {"city": "杭州", "items": [1, None]}, ttl_seconds=60
        )
        second = self.serializer.dumps(
            {"items": [1, None], "city": "杭州"}, ttl_seconds=60
        )
        envelope = self.serializer.loads(first)

        self.assertEqual(first, second)
        self.assertIn("杭州".encode("utf-8"), first)
        self.assertEqual(envelope.schema_version, 1)
        self.assertEqual(envelope.created_at, self.now)
        self.assertEqual(envelope.expires_at, self.now + timedelta(seconds=60))
        self.assertEqual(envelope.payload, {"city": "杭州", "items": [1, None]})

    def test_supported_domain_types_follow_json_rules(self):
        payload = {
            "model": ExampleModel(city="杭州", price=Decimal("12.30")),
            "dataclass": ExampleDataclass("西湖", self.now),
            "date": self.now.date(),
            "time": self.now.time(),
            "decimal": Decimal("9.90"),
            "enum": Color.RED,
        }

        loaded = self.serializer.loads(
            self.serializer.dumps(payload, ttl_seconds=30)
        ).payload

        self.assertEqual(loaded["model"], {"city": "杭州", "price": "12.30"})
        self.assertEqual(loaded["dataclass"]["name"], "西湖")
        self.assertEqual(loaded["decimal"], "9.90")
        self.assertEqual(loaded["enum"], "red")

    def test_invalid_json_envelope_and_version_are_rejected(self):
        with self.assertRaises(CacheSerializationError):
            self.serializer.loads(b"\xff")
        with self.assertRaises(CacheSerializationError):
            self.serializer.loads("[]")
        with self.assertRaises(CacheSerializationError):
            self.serializer.loads('{"schema_version":1}')

        different_version = CacheEnvelopeSerializer(2, clock=lambda: self.now)
        raw = different_version.dumps({"city": "杭州"}, ttl_seconds=10)
        with self.assertRaises(CacheSchemaVersionError):
            self.serializer.loads(raw)

    def test_envelope_rejects_extra_fields_naive_time_and_invalid_range(self):
        base = {
            "schema_version": 1,
            "created_at": self.now.isoformat(),
            "expires_at": (self.now + timedelta(seconds=10)).isoformat(),
            "payload": {},
        }
        with self.assertRaises(CacheSerializationError):
            self.serializer.loads(json.dumps({**base, "unexpected": True}))

        naive = {**base, "created_at": "2026-08-20T08:30:00"}
        with self.assertRaises(CacheSerializationError):
            self.serializer.loads(json.dumps(naive))

        invalid_range = {**base, "expires_at": self.now.isoformat()}
        with self.assertRaises(CacheSerializationError):
            self.serializer.loads(json.dumps(invalid_range))

    def test_unsupported_values_nan_and_invalid_ttl_are_rejected(self):
        with self.assertRaises(CacheSerializationError):
            self.serializer.dumps({"value": object()}, ttl_seconds=10)
        with self.assertRaises(CacheSerializationError):
            self.serializer.dumps({"value": float("nan")}, ttl_seconds=10)
        with self.assertRaises(CacheSerializationError):
            self.serializer.dumps({}, ttl_seconds=True)

    def test_expiration_uses_injected_clock(self):
        envelope = self.serializer.loads(
            self.serializer.dumps({"city": "杭州"}, ttl_seconds=10)
        )
        self.assertFalse(self.serializer.is_expired(envelope))

        self.now += timedelta(seconds=10)

        self.assertTrue(self.serializer.is_expired(envelope))


class CacheTTLPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = CacheTTLPolicy(
            default_seconds=60,
            min_seconds=5,
            max_seconds=300,
        )

    def test_default_minimum_maximum_and_non_positive_rules(self):
        self.assertEqual(self.policy.resolve(None).seconds, 60)
        self.assertEqual(self.policy.resolve(1).seconds, 5)
        self.assertEqual(self.policy.resolve(600).seconds, 300)
        self.assertFalse(self.policy.resolve(0).cacheable)
        self.assertFalse(self.policy.resolve(-1).cacheable)

    def test_non_integer_values_are_rejected(self):
        with self.assertRaises(TypeError):
            self.policy.resolve(True)
        with self.assertRaises(TypeError):
            self.policy.resolve(1.5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            CacheTTLPolicy(default_seconds=True)  # type: ignore[arg-type]


class NoOpCacheStoreTests(unittest.TestCase):
    def test_noop_returns_explicit_bypass_and_tracks_metrics(self):
        store = NoOpCacheStore(schema_version=3)

        lookup = store.get("any")
        write = store.set("any", {"city": "杭州"})
        deleted = store.delete("any")
        metrics = store.metrics_snapshot()

        self.assertIsInstance(store, CacheStore)
        self.assertEqual(store.schema_version, 3)
        self.assertEqual(lookup.status, CacheReadStatus.BYPASS)
        self.assertEqual(write.status, CacheWriteStatus.SKIPPED)
        self.assertFalse(deleted)
        self.assertEqual(metrics.read_requests, 1)
        self.assertEqual(metrics.bypasses, 1)
        self.assertEqual(metrics.write_requests, 1)
        self.assertEqual(metrics.skipped_writes, 1)
        self.assertEqual(metrics.delete_requests, 1)

    def test_noop_uses_the_same_key_and_schema_validation(self):
        store = NoOpCacheStore()
        with self.assertRaises(ValueError):
            store.get("")
        with self.assertRaises(ValueError):
            NoOpCacheStore(schema_version=0)
        with self.assertRaises(TypeError):
            NoOpCacheStore(schema_version=True)


class RedisCacheStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 20, 8, 30, tzinfo=timezone.utc)
        self.client = FakeCacheRedis()
        self.manager = RedisClientManager(
            RedisConfig(enabled=True, degrade_cooldown_seconds=0),
            client_factory=lambda _: self.client,
        )
        self.store = RedisCacheStore(
            self.manager,
            serializer=CacheEnvelopeSerializer(1, clock=lambda: self.now),
            ttl_policy=CacheTTLPolicy(
                default_seconds=60,
                min_seconds=5,
                max_seconds=300,
            ),
        )

    def tearDown(self) -> None:
        self.manager.close()

    def test_miss_set_hit_and_cached_null(self):
        self.assertEqual(self.store.get("cache:key").status, CacheReadStatus.MISS)

        stored = self.store.set("cache:key", {"city": "杭州"})
        lookup = self.store.get("cache:key")
        null_stored = self.store.set("cache:null", None)
        null_lookup = self.store.get("cache:null")

        self.assertEqual(stored.status, CacheWriteStatus.STORED)
        self.assertEqual(stored.ttl_seconds, 60)
        self.assertEqual(self.client.ttls["cache:key"], 60)
        self.assertEqual(lookup.status, CacheReadStatus.HIT)
        self.assertEqual(lookup.value, {"city": "杭州"})
        self.assertEqual(null_stored.status, CacheWriteStatus.STORED)
        self.assertTrue(null_lookup.hit)
        self.assertIsNone(null_lookup.value)

    def test_ttl_is_clamped_or_skipped(self):
        minimum = self.store.set("cache:min", {}, ttl_seconds=1)
        maximum = self.store.set("cache:max", {}, ttl_seconds=999)
        skipped = self.store.set("cache:skip", {}, ttl_seconds=0)

        self.assertEqual(minimum.ttl_seconds, 5)
        self.assertEqual(minimum.reason, "clamped_to_minimum")
        self.assertEqual(maximum.ttl_seconds, 300)
        self.assertEqual(maximum.reason, "clamped_to_maximum")
        self.assertEqual(skipped.status, CacheWriteStatus.SKIPPED)
        self.assertNotIn("cache:skip", self.client.data)

    def test_redis_failures_return_degraded_results(self):
        self.client.get_error = RedisConnectionError("offline")
        read = self.store.get("cache:get")
        self.client.get_error = None
        self.client.set_error = RedisConnectionError("offline")
        write = self.store.set("cache:set", {})
        self.client.set_error = None
        self.client.delete_error = RedisConnectionError("offline")
        deleted = self.store.delete("cache:delete")

        self.assertEqual(read.status, CacheReadStatus.DEGRADED)
        self.assertEqual(write.status, CacheWriteStatus.DEGRADED)
        self.assertFalse(deleted)
        metrics = self.store.metrics_snapshot()
        self.assertEqual(metrics.degraded_reads, 1)
        self.assertEqual(metrics.degraded_writes, 1)
        self.assertEqual(metrics.degraded_deletes, 1)

    def test_invalid_old_and_expired_entries_become_misses_and_are_evicted(self):
        self.client.data["cache:broken"] = b"not-json"
        old_serializer = CacheEnvelopeSerializer(2, clock=lambda: self.now)
        self.client.data["cache:old"] = old_serializer.dumps({}, ttl_seconds=60)
        self.client.data["cache:expired"] = self.store.serializer.dumps(
            {}, ttl_seconds=1
        )
        self.now += timedelta(seconds=2)

        broken = self.store.get("cache:broken")
        old = self.store.get("cache:old")
        expired = self.store.get("cache:expired")

        self.assertEqual(broken.reason, "invalid_entry")
        self.assertEqual(old.reason, "invalid_entry")
        self.assertEqual(expired.reason, "expired_entry")
        self.assertNotIn("cache:broken", self.client.data)
        self.assertNotIn("cache:old", self.client.data)
        self.assertNotIn("cache:expired", self.client.data)
        metrics = self.store.metrics_snapshot()
        self.assertEqual(metrics.misses, 3)
        self.assertEqual(metrics.invalid_entries, 2)
        self.assertEqual(metrics.expired_entries, 1)
        self.assertEqual(metrics.deletes, 3)

    def test_invalid_entry_can_be_left_for_redis_ttl_cleanup(self):
        store = RedisCacheStore(
            self.manager,
            serializer=CacheEnvelopeSerializer(1, clock=lambda: self.now),
            ttl_policy=CacheTTLPolicy(),
            delete_invalid_entries=False,
        )
        self.client.data["cache:broken"] = b"not-json"

        result = store.get("cache:broken")

        self.assertEqual(result.status, CacheReadStatus.MISS)
        self.assertIn("cache:broken", self.client.data)
        self.assertEqual(store.metrics_snapshot().delete_requests, 0)

    def test_serialization_error_is_not_hidden_as_redis_degradation(self):
        with self.assertRaises(CacheSerializationError):
            self.store.set("cache:key", object())

        metrics = self.store.metrics_snapshot()
        self.assertEqual(metrics.skipped_writes, 1)
        self.assertEqual(metrics.degraded_writes, 0)

    def test_key_validation_and_delete(self):
        with self.assertRaises(ValueError):
            self.store.get("")
        with self.assertRaises(ValueError):
            self.store.set("中" * 300, {})

        self.store.set("cache:key", {})
        self.assertTrue(self.store.delete("cache:key"))
        self.assertFalse(self.store.delete("cache:key"))

    def test_metrics_count_hit_miss_and_hit_rate(self):
        self.store.get("cache:missing")
        self.store.set("cache:hit", {})
        self.store.get("cache:hit")
        self.store.get("cache:hit")

        metrics = self.store.metrics_snapshot()

        self.assertEqual(metrics.read_requests, 3)
        self.assertEqual(metrics.hits, 2)
        self.assertEqual(metrics.misses, 1)
        self.assertEqual(metrics.hit_rate, 0.666667)
        self.assertEqual(metrics.write_requests, 1)
        self.assertEqual(metrics.writes, 1)


class CacheFactoryTests(unittest.TestCase):
    def test_disabled_cache_returns_noop_without_creating_redis_client(self):
        manager = RedisClientManager(
            RedisConfig(enabled=False),
            client_factory=lambda _: (_ for _ in ()).throw(
                AssertionError("不应创建 Redis 客户端")
            ),
        )

        store = create_cache_store(
            cache_config=CacheConfig(enabled=False),
            redis_client_manager=manager,
        )

        self.assertIsInstance(store, NoOpCacheStore)
        self.assertEqual(store.get("key").status, CacheReadStatus.BYPASS)

    def test_enabled_cache_returns_redis_store_without_network_access(self):
        manager = RedisClientManager(
            RedisConfig(enabled=True),
            client_factory=lambda _: (_ for _ in ()).throw(
                AssertionError("构建 Store 时不应连接 Redis")
            ),
        )

        store = create_cache_store(
            cache_config=CacheConfig(enabled=True),
            redis_client_manager=manager,
        )

        self.assertIsInstance(store, RedisCacheStore)
        self.assertIsInstance(store, CacheStore)


if __name__ == "__main__":
    unittest.main()
