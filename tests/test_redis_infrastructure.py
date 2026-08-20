"""Redis 阶段一基础设施测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from redis import Connection, SSLConnection
from redis.exceptions import ConnectionError as RedisConnectionError

from app.infrastructure.redis import (
    RedisClientManager,
    RedisConfig,
    RedisHealthStatus,
    RedisKeyBuilder,
    create_redis_client,
)


class FakePool:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    def disconnect(self) -> None:
        self.disconnect_calls += 1


class FakeRedis:
    def __init__(self, *, ping_result=True, ping_error: Exception | None = None) -> None:
        self.ping_result = ping_result
        self.ping_error = ping_error
        self.connection_pool = FakePool()
        self.close_calls = 0

    def ping(self):
        if self.ping_error is not None:
            raise self.ping_error
        return self.ping_result

    def close(self) -> None:
        self.close_calls += 1


class RedisConfigTests(unittest.TestCase):
    def test_config_reads_all_runtime_settings(self):
        settings = SimpleNamespace(
            REDIS_ENABLED=True,
            REDIS_HOST="redis.internal",
            REDIS_PORT=6380,
            REDIS_DB=2,
            REDIS_USERNAME="app",
            REDIS_PASSWORD="secret",
            REDIS_SSL=True,
            REDIS_MAX_CONNECTIONS=31,
            REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS=1.5,
            REDIS_SOCKET_TIMEOUT_SECONDS=4.5,
            REDIS_HEALTH_CHECK_INTERVAL_SECONDS=20,
            REDIS_RETRY_ON_TIMEOUT=False,
            REDIS_DECODE_RESPONSES=True,
            REDIS_CLIENT_NAME="trip-worker",
            REDIS_KEY_PREFIX="travel-agent:test",
            REDIS_DEFAULT_TTL_SECONDS=600,
            REDIS_DEGRADE_COOLDOWN_SECONDS=7.0,
        )

        config = RedisConfig.from_settings(settings)

        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "redis.internal")
        self.assertEqual(config.port, 6380)
        self.assertEqual(config.database, 2)
        self.assertEqual(config.max_connections, 31)
        self.assertEqual(config.default_ttl_seconds, 600)
        self.assertEqual(config.safe_target(), "rediss://redis.internal:6380/2")
        self.assertNotIn("secret", config.safe_target())

    def test_connection_pool_receives_timeout_and_capacity_options(self):
        config = RedisConfig(
            enabled=True,
            ssl=False,
            max_connections=12,
            socket_connect_timeout_seconds=2.0,
            socket_timeout_seconds=8.0,
            health_check_interval_seconds=45,
            retry_on_timeout=True,
        )
        sentinel_client = object()

        with patch("app.infrastructure.redis.client.ConnectionPool") as pool_mock, patch(
            "app.infrastructure.redis.client.Redis",
            return_value=sentinel_client,
        ) as redis_mock:
            pool = Mock()
            pool_mock.return_value = pool
            result = create_redis_client(config)

        self.assertIs(result, sentinel_client)
        kwargs = pool_mock.call_args.kwargs
        self.assertIs(kwargs["connection_class"], Connection)
        self.assertEqual(kwargs["max_connections"], 12)
        self.assertEqual(kwargs["socket_connect_timeout"], 2.0)
        self.assertEqual(kwargs["socket_timeout"], 8.0)
        self.assertEqual(kwargs["health_check_interval"], 45)
        self.assertTrue(kwargs["retry_on_timeout"])
        redis_mock.assert_called_once_with(connection_pool=pool)

    def test_ssl_uses_ssl_connection_class(self):
        with patch("app.infrastructure.redis.client.ConnectionPool") as pool_mock, patch(
            "app.infrastructure.redis.client.Redis"
        ):
            create_redis_client(RedisConfig(enabled=True, ssl=True))

        self.assertIs(pool_mock.call_args.kwargs["connection_class"], SSLConnection)


class RedisKeyBuilderTests(unittest.TestCase):
    def test_hashed_keys_are_stable_and_do_not_expose_query_text(self):
        builder = RedisKeyBuilder("travel-agent:test")
        first = builder.route(
            {"origin": "杭州西湖", "destination": "灵隐寺", "mode": "transit"}
        )
        second = builder.route(
            {"mode": "transit", "destination": "灵隐寺", "origin": "杭州西湖"}
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("travel-agent:test:cache:route:"))
        self.assertNotIn("杭州", first)
        self.assertNotIn("灵隐寺", first)
        self.assertEqual(len(first.rsplit(":", 1)[1]), 64)

    def test_literal_keys_use_explicit_namespaces(self):
        builder = RedisKeyBuilder("travel-agent:dev")

        self.assertEqual(
            builder.task_progress("a3b0c1d2"),
            "travel-agent:dev:task:progress:a3b0c1d2",
        )
        self.assertEqual(
            builder.session("session-1"),
            "travel-agent:dev:session:session-1",
        )

    def test_invalid_literal_and_empty_prefix_are_rejected(self):
        with self.assertRaises(ValueError):
            RedisKeyBuilder("")
        builder = RedisKeyBuilder("travel-agent:test")
        with self.assertRaises(ValueError):
            builder.literal("session", "contains space")


class RedisClientManagerTests(unittest.TestCase):
    def test_disabled_manager_never_creates_client(self):
        factory = Mock()
        manager = RedisClientManager(
            RedisConfig(enabled=False),
            client_factory=factory,
        )

        self.assertIsNone(manager.get_client())
        self.assertEqual(manager.execute(lambda _: "redis", fallback="mysql"), "mysql")
        health = manager.check_health()

        factory.assert_not_called()
        self.assertEqual(health.status, RedisHealthStatus.DISABLED)
        self.assertIsNone(health.healthy)
        self.assertFalse(health.degraded)

    def test_execute_returns_fallback_and_enters_cooldown_on_redis_failure(self):
        clock = [100.0]
        first_client = FakeRedis()
        second_client = FakeRedis()
        factory = Mock(side_effect=[first_client, second_client])
        manager = RedisClientManager(
            RedisConfig(enabled=True, degrade_cooldown_seconds=5),
            client_factory=factory,
            monotonic=lambda: clock[0],
        )

        result = manager.execute(
            lambda _: (_ for _ in ()).throw(RedisConnectionError("offline")),
            fallback="mysql",
        )

        self.assertEqual(result, "mysql")
        self.assertIsNone(manager.get_client())
        self.assertEqual(first_client.close_calls, 1)
        self.assertEqual(first_client.connection_pool.disconnect_calls, 1)
        self.assertEqual(factory.call_count, 1)

        clock[0] = 106.0
        self.assertIs(manager.get_client(), second_client)
        self.assertEqual(factory.call_count, 2)

    def test_client_factory_failure_also_returns_fallback(self):
        manager = RedisClientManager(
            RedisConfig(enabled=True),
            client_factory=Mock(side_effect=RedisConnectionError("cannot connect")),
        )

        result = manager.execute(lambda _: "redis", fallback="mysql")

        self.assertEqual(result, "mysql")
        self.assertIsNone(manager.get_client())

    def test_health_check_recovers_immediately_and_redacts_password(self):
        clock = [10.0]
        password = "p@ss/word"
        failing = FakeRedis(
            ping_error=RedisConnectionError(
                "failed p@ss/word redis://:p%40ss%2Fword@localhost"
            )
        )
        recovered = FakeRedis()
        manager = RedisClientManager(
            RedisConfig(
                enabled=True,
                password=password,
                degrade_cooldown_seconds=60,
            ),
            client_factory=Mock(side_effect=[failing, recovered]),
            monotonic=lambda: clock[0],
        )

        failed_health = manager.check_health()
        recovered_health = manager.check_health()

        self.assertEqual(failed_health.status, RedisHealthStatus.DEGRADED)
        self.assertFalse(failed_health.healthy)
        self.assertNotIn(password, failed_health.error or "")
        self.assertNotIn("p%40ss%2Fword", failed_health.error or "")
        self.assertIn("***", failed_health.error or "")
        self.assertEqual(recovered_health.status, RedisHealthStatus.OK)
        self.assertTrue(recovered_health.healthy)
        self.assertFalse(recovered_health.degraded)

    def test_close_releases_connection_pool(self):
        client = FakeRedis()
        manager = RedisClientManager(
            RedisConfig(enabled=True),
            client_factory=lambda _: client,
        )
        manager.get_client()

        manager.close()
        manager.close()

        self.assertEqual(client.close_calls, 1)
        self.assertEqual(client.connection_pool.disconnect_calls, 1)


if __name__ == "__main__":
    unittest.main()
