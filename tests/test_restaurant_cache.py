import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from app.memory import SQLiteRestaurantCache
from app.providers.amap.client import AmapProviderClient
from app.providers.amap.models import (
    GeoPoint,
    PoiCandidate,
    RestaurantSearchAnchor,
    RestaurantSearchSnapshot,
)
from app.providers.amap.restaurant_cache import restaurant_search_cache_key


def make_snapshot() -> RestaurantSearchSnapshot:
    return RestaurantSearchSnapshot(
        query_city="杭州",
        keywords="餐厅",
        center=GeoPoint(longitude=120.15, latitude=30.25),
        radius_meters=2500,
        page_size=4,
        total_received=1,
        candidates=[
            PoiCandidate(
                poi_id="food-1",
                name="湖畔餐厅",
                address="湖滨路1号",
                location=GeoPoint(longitude=120.151, latitude=30.251),
                opening_hours="10:00-22:00",
                distance_meters=120,
            )
        ],
    )


def make_anchor(longitude: float = 120.15) -> RestaurantSearchAnchor:
    return RestaurantSearchAnchor(
        anchor_id="day-0-lunch",
        day_index=0,
        meal_type="lunch",
        name="西湖",
        location=GeoPoint(longitude=longitude, latitude=30.25),
    )


class RestaurantCacheKeyTests(unittest.TestCase):
    def test_key_is_stable_and_isolates_search_affecting_fields(self):
        base = restaurant_search_cache_key(
            city="杭州",
            keywords="餐厅",
            center=GeoPoint(longitude=120.15, latitude=30.25),
            radius_meters=2500,
            page_size=4,
        )
        same = restaurant_search_cache_key(
            city=" 杭州 ",
            keywords="餐厅",
            center=GeoPoint(longitude=120.15, latitude=30.25),
            radius_meters=2500,
            page_size=4,
        )
        changed_radius = restaurant_search_cache_key(
            city="杭州",
            keywords="餐厅",
            center=GeoPoint(longitude=120.15, latitude=30.25),
            radius_meters=3000,
            page_size=4,
        )
        changed_keyword = restaurant_search_cache_key(
            city="杭州",
            keywords="咖啡",
            center=GeoPoint(longitude=120.15, latitude=30.25),
            radius_meters=2500,
            page_size=4,
        )

        self.assertEqual(base, same)
        self.assertNotEqual(base, changed_radius)
        self.assertNotEqual(base, changed_keyword)


class SQLiteRestaurantCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        self.cache = SQLiteRestaurantCache(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_value_survives_restart_and_expired_value_is_deleted(self):
        self.cache.set("restaurant-key", make_snapshot(), ttl_seconds=3600)
        reopened = SQLiteRestaurantCache(self.db_path)

        self.assertEqual(reopened.get("restaurant-key").candidates[0].poi_id, "food-1")

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE restaurant_cache SET expires_at = ? WHERE cache_key = ?",
                ("2000-01-01T00:00:00+00:00", "restaurant-key"),
            )
            connection.commit()
        self.assertIsNone(reopened.get("restaurant-key"))

    def test_purge_expired_only_removes_expired_rows(self):
        self.cache.set("expired", make_snapshot(), ttl_seconds=3600)
        self.cache.set("active", make_snapshot(), ttl_seconds=3600)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE restaurant_cache SET expires_at = ? WHERE cache_key = ?",
                ("2000-01-01T00:00:00+00:00", "expired"),
            )
            connection.commit()

        self.assertEqual(self.cache.purge_expired(), 1)
        self.assertIsNotNone(self.cache.get("active"))


class RestaurantProviderCacheTests(unittest.TestCase):
    def test_second_provider_instance_uses_sqlite_without_raw_http(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SQLiteRestaurantCache(Path(temp_dir) / "memory.db")

            class RawClient:
                calls = 0

                @classmethod
                def around_search(cls, **kwargs):
                    cls.calls += 1
                    return {
                        "status": "1",
                        "info": "OK",
                        "pois": [
                            {
                                "id": "food-1",
                                "name": "湖畔餐厅",
                                "address": "湖滨路1号",
                                "location": "120.151,30.251",
                                "distance": "120",
                                "business": {
                                    "rating": "4.8",
                                    "opentime_today": "10:00-22:00",
                                },
                            }
                        ],
                    }

            first = AmapProviderClient(raw_client=RawClient, restaurant_cache=cache)
            first_result = first.search_restaurants(city="杭州", anchors=[make_anchor()])
            second = AmapProviderClient(raw_client=RawClient, restaurant_cache=cache)
            second_result = second.search_restaurants(city="杭州", anchors=[make_anchor()])

            self.assertEqual(RawClient.calls, 1)
            self.assertEqual(first_result.cache_misses, 1)
            self.assertEqual(second_result.cache_hits, 1)
            self.assertEqual(second_result.candidates[0].day_index, 0)
            self.assertEqual(second_result.candidates[0].meal_type, "lunch")

    def test_different_coordinates_do_not_share_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SQLiteRestaurantCache(Path(temp_dir) / "memory.db")

            class RawClient:
                calls = 0

                @classmethod
                def around_search(cls, **kwargs):
                    cls.calls += 1
                    return {"status": "1", "info": "OK", "pois": []}

            provider = AmapProviderClient(raw_client=RawClient, restaurant_cache=cache)
            provider.search_restaurants(city="杭州", anchors=[make_anchor(120.15)])
            provider.search_restaurants(city="杭州", anchors=[make_anchor(120.16)])

            self.assertEqual(RawClient.calls, 2)


if __name__ == "__main__":
    unittest.main()
