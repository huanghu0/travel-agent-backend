import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from app.memory import SQLiteRouteCache
from app.providers.amap.models import RouteEstimate, RouteLegRequest
from app.providers.amap.route_cache import route_leg_cache_key


def make_leg(*, mode: str = "walking", longitude: float = 104.01) -> RouteLegRequest:
    return RouteLegRequest.model_validate(
        {
            "day_index": 0,
            "leg_index": 0,
            "date": "2026-08-10",
            "origin": {
                "name": "A",
                "poi_id": "poi-a",
                "city_code": "028",
                "location": {"longitude": longitude, "latitude": 30.61},
            },
            "destination": {
                "name": "B",
                "poi_id": "poi-b",
                "city_code": "028",
                "location": {"longitude": 104.02, "latitude": 30.62},
            },
            "mode": mode,
        }
    )


def make_estimate() -> RouteEstimate:
    return RouteEstimate(
        day_index=0,
        leg_index=0,
        date="2026-08-10",
        origin_name="A",
        destination_name="B",
        mode="walking",
        distance_meters=1200,
        duration_seconds=900,
    )


class RouteCacheKeyTests(unittest.TestCase):
    def test_key_is_stable_and_ignores_display_metadata(self):
        leg = make_leg()
        renamed = leg.model_copy(
            update={
                "day_index": 4,
                "leg_index": 2,
                "date": "2026-09-01",
                "origin": leg.origin.model_copy(update={"name": "Renamed A"}),
            }
        )

        self.assertEqual(route_leg_cache_key(leg), route_leg_cache_key(renamed))

    def test_key_changes_for_route_affecting_fields(self):
        base = make_leg()
        self.assertNotEqual(
            route_leg_cache_key(base),
            route_leg_cache_key(make_leg(mode="driving")),
        )
        self.assertNotEqual(
            route_leg_cache_key(base),
            route_leg_cache_key(make_leg(longitude=104.011)),
        )


class SQLiteRouteCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        self.cache = SQLiteRouteCache(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_value_survives_cache_instance_restart(self):
        self.cache.set("route-key", make_estimate(), ttl_seconds=3600)

        reopened = SQLiteRouteCache(self.db_path)
        cached = reopened.get("route-key")

        self.assertIsNotNone(cached)
        self.assertEqual(cached.distance_meters, 1200)
        self.assertFalse(cached.cache_hit)

    def test_expired_value_is_deleted_and_returns_miss(self):
        self.cache.set("route-key", make_estimate(), ttl_seconds=3600)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE route_cache SET expires_at = ? WHERE cache_key = ?",
                ("2000-01-01T00:00:00+00:00", "route-key"),
            )
            connection.commit()

        self.assertIsNone(self.cache.get("route-key"))
        with closing(sqlite3.connect(self.db_path)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM route_cache WHERE cache_key = ?",
                ("route-key",),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_non_positive_ttl_is_not_persisted(self):
        self.cache.set("route-key", make_estimate(), ttl_seconds=0)
        self.assertIsNone(self.cache.get("route-key"))


if __name__ == "__main__":
    unittest.main()
