import tempfile
import unittest
from pathlib import Path

from app.core.config import settings
from app.memory import SQLiteRouteCache
from app.providers.amap.client import AmapProviderClient
from app.providers.amap.errors import AmapErrorKind, AmapProviderError
from app.routing import (
    build_route_legs,
    normalize_transportation_mode,
    plan_route_fingerprint,
)
from app.schemas.trip_schema import TripPlan, TripRequest


def make_request() -> TripRequest:
    return TripRequest(
        city="Chengdu",
        start_date="2026-08-10",
        end_date="2026-08-10",
        travel_days=1,
        transportation="public transit",
        accommodation="hotel",
        preferences=[],
    )


def make_plan() -> TripPlan:
    return TripPlan.model_validate(
        {
            "city": "Chengdu",
            "start_date": "2026-08-10",
            "end_date": "2026-08-10",
            "days": [
                {
                    "date": "2026-08-10",
                    "day_index": 0,
                    "description": "city route",
                    "transportation": "walking + metro",
                    "accommodation": "hotel",
                    "attractions": [
                        {
                            "name": "A",
                            "address": "address A",
                            "location": {"longitude": 104.01, "latitude": 30.61},
                            "visit_duration": 60,
                            "description": "A",
                        },
                        {
                            "name": "B",
                            "address": "address B",
                            "location": {"longitude": 104.02, "latitude": 30.62},
                            "visit_duration": 60,
                            "description": "B",
                        },
                        {
                            "name": "C",
                            "address": "address C",
                            "location": {"longitude": 104.03, "latitude": 30.63},
                            "visit_duration": 60,
                            "description": "C",
                        },
                    ],
                    "meals": [],
                }
            ],
            "weather_info": [],
            "overall_suggestions": "book ahead",
            "budget": None,
        }
    )


class RoutePlanningTests(unittest.TestCase):
    def test_transportation_mode_mapping_prefers_transit_for_combined_text(self):
        cases = {
            "\u516c\u5171\u4ea4\u901a": "transit",
            "walking + metro": "transit",
            "\u6b65\u884c+\u5730\u94c1": "transit",
            "\u81ea\u9a7e": "driving",
            "taxi": "driving",
            "\u5f92\u6b65": "walking",
            "walk": "walking",
            "unknown": "transit",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_transportation_mode(value), expected)

    def test_explicit_mode_declaration_wins_over_route_segment_text(self):
        # 重建文案中可能包含与主方式不同的降级分段，路线指纹必须遵循显式声明。
        description = (
            "\u51fa\u884c\u65b9\u5f0f\uff1a\u9a7e\u8f66\uff1b"
            "A\u2192B\uff08\u516c\u5171\u4ea4\u901a\u7ea620\u5206\u949f\uff09\u3002"
        )

        self.assertEqual(normalize_transportation_mode(description), "driving")

    def test_fingerprint_is_stable_and_changes_when_attraction_order_changes(self):
        request = make_request()
        plan = make_plan()

        first = plan_route_fingerprint(request, plan)
        second = plan_route_fingerprint(request, plan.model_copy(deep=True))
        reversed_plan = plan.model_copy(deep=True)
        reversed_plan.days[0].attractions.reverse()

        self.assertEqual(first, second)
        self.assertNotEqual(first, plan_route_fingerprint(request, reversed_plan))

    def test_only_adjacent_legs_are_built_and_candidate_metadata_is_reused(self):
        legs = build_route_legs(
            make_request(),
            make_plan(),
            attractions={
                "provider": "amap",
                "candidates": [
                    {"poi_id": "poi-a", "name": "A", "city_code": "028"},
                    {"poi_id": "poi-b", "name": "B", "city_code": "028"},
                    {"poi_id": "poi-c", "name": "C", "city_code": "028"},
                ],
            },
        )

        self.assertEqual(len(legs), 2)
        self.assertEqual(
            [(item.origin.name, item.destination.name) for item in legs],
            [("A", "B"), ("B", "C")],
        )
        self.assertTrue(all(item.mode == "transit" for item in legs))
        self.assertEqual(legs[0].origin.poi_id, "poi-a")
        self.assertEqual(legs[0].destination.poi_id, "poi-b")
        self.assertEqual(legs[0].origin.city_code, "028")


class AmapRouteProviderTests(unittest.TestCase):
    def test_transit_city_code_is_resolved_once_and_cached(self):
        class RawClient:
            district_calls = 0
            route_calls = []

            @classmethod
            def district_search(cls, city):
                cls.district_calls += 1
                return {
                    "status": "1",
                    "info": "OK",
                    "districts": [{"citycode": "028"}],
                }

            @classmethod
            def route(cls, **kwargs):
                cls.route_calls.append(kwargs)
                return {
                    "status": "1",
                    "info": "OK",
                    "route": {
                        "transits": [
                            {"distance": "1234", "cost": {"duration": "900"}}
                        ]
                    },
                }

        provider = AmapProviderClient(raw_client=RawClient)
        legs = build_route_legs(make_request(), make_plan())
        result = provider.estimate_routes(
            city="Chengdu",
            plan_fingerprint="fingerprint",
            legs=legs,
        )

        self.assertEqual(RawClient.district_calls, 1)
        self.assertEqual(len(RawClient.route_calls), 2)
        self.assertTrue(
            all(
                call["origin_city_code"] == "028"
                and call["destination_city_code"] == "028"
                for call in RawClient.route_calls
            )
        )
        self.assertEqual(result.requested_legs, 2)
        self.assertEqual(result.evaluated_legs, 2)
        self.assertEqual(result.routes[0].distance_meters, 1234)
        self.assertEqual(result.routes[0].duration_seconds, 900)

    def test_route_leg_limit_is_reported_without_querying_truncated_legs(self):
        class RawClient:
            route_calls = 0

            @classmethod
            def district_search(cls, city):
                return {"status": "1", "districts": [{"citycode": "028"}]}

            @classmethod
            def route(cls, **kwargs):
                cls.route_calls += 1
                return {
                    "status": "1",
                    "route": {
                        "transits": [
                            {"distance": "10", "cost": {"duration": "20"}}
                        ]
                    },
                }

        provider = AmapProviderClient(raw_client=RawClient)
        result = provider.estimate_routes(
            city="Chengdu",
            plan_fingerprint="fingerprint",
            legs=build_route_legs(make_request(), make_plan()),
            limit=1,
        )

        self.assertEqual(RawClient.route_calls, 1)
        self.assertEqual(result.requested_legs, 2)
        self.assertEqual(result.evaluated_legs, 1)
        self.assertEqual(result.truncated_legs, 1)


    def test_one_failed_leg_does_not_discard_other_route_results(self):
        class RawClient:
            route_calls = 0

            @classmethod
            def district_search(cls, city):
                return {"status": "1", "districts": [{"citycode": "028"}]}

            @classmethod
            def route(cls, **kwargs):
                cls.route_calls += 1
                if cls.route_calls == 2:
                    raise AmapProviderError(
                        "route timeout",
                        kind=AmapErrorKind.TIMEOUT,
                        retryable=True,
                    )
                return {
                    "status": "1",
                    "route": {
                        "transits": [
                            {"distance": "100", "cost": {"duration": "200"}}
                        ]
                    },
                }

        legs = build_route_legs(make_request(), make_plan())
        third = legs[1].model_copy(
            update={
                "leg_index": 2,
                "origin": legs[0].origin,
                "destination": legs[1].destination,
            }
        )
        result = AmapProviderClient(raw_client=RawClient).estimate_routes(
            city="Chengdu",
            plan_fingerprint="fingerprint",
            legs=[*legs, third],
        )

        self.assertEqual(len(result.routes), 3)
        self.assertTrue(result.routes[0].available)
        self.assertFalse(result.routes[1].available)
        self.assertEqual(result.routes[1].error_code, "AMAP_TIMEOUT")
        self.assertTrue(result.routes[2].available)
        self.assertEqual(result.failed_legs, 1)

    def test_authorization_and_rate_limit_errors_fail_the_batch(self):
        for kind in (AmapErrorKind.AUTHORIZATION, AmapErrorKind.RATE_LIMIT):
            class RawClient:
                @classmethod
                def district_search(cls, city):
                    return {"status": "1", "districts": [{"citycode": "028"}]}

                @classmethod
                def route(cls, **kwargs):
                    raise AmapProviderError(
                        kind.value,
                        kind=kind,
                        retryable=kind == AmapErrorKind.RATE_LIMIT,
                    )

            with self.subTest(kind=kind):
                with self.assertRaises(AmapProviderError) as raised:
                    AmapProviderClient(raw_client=RawClient).estimate_routes(
                        city="Chengdu",
                        plan_fingerprint="fingerprint",
                        legs=build_route_legs(make_request(), make_plan()),
                    )
                self.assertEqual(raised.exception.kind, kind)

    def test_sqlite_cache_avoids_repeating_successful_route_calls(self):
        class RawClient:
            district_calls = 0
            route_calls = 0

            @classmethod
            def district_search(cls, city):
                cls.district_calls += 1
                return {"status": "1", "districts": [{"citycode": "028"}]}

            @classmethod
            def route(cls, **kwargs):
                cls.route_calls += 1
                return {
                    "status": "1",
                    "route": {
                        "transits": [
                            {"distance": "123", "cost": {"duration": "456"}}
                        ]
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SQLiteRouteCache(Path(temp_dir) / "memory.db")
            provider = AmapProviderClient(raw_client=RawClient, route_cache=cache)
            legs = build_route_legs(make_request(), make_plan())

            first = provider.estimate_routes(
                city="Chengdu",
                plan_fingerprint="first",
                legs=legs,
            )
            second = provider.estimate_routes(
                city="Chengdu",
                plan_fingerprint="second",
                legs=legs,
            )

        self.assertEqual(RawClient.route_calls, 2)
        self.assertEqual(first.cache_misses, 2)
        self.assertEqual(first.cache_hits, 0)
        self.assertEqual(second.cache_hits, 2)
        self.assertEqual(second.cache_misses, 0)
        self.assertTrue(all(route.cache_hit for route in second.routes))

    def test_retry_after_rate_limit_reuses_earlier_successful_leg(self):
        class RawClient:
            route_calls = 0

            @classmethod
            def district_search(cls, city):
                return {"status": "1", "districts": [{"citycode": "028"}]}

            @classmethod
            def route(cls, **kwargs):
                cls.route_calls += 1
                if cls.route_calls == 2:
                    raise AmapProviderError(
                        "rate limited",
                        kind=AmapErrorKind.RATE_LIMIT,
                        retryable=True,
                    )
                return {
                    "status": "1",
                    "route": {
                        "transits": [
                            {"distance": "100", "cost": {"duration": "200"}}
                        ]
                    },
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = SQLiteRouteCache(Path(temp_dir) / "memory.db")
            provider = AmapProviderClient(raw_client=RawClient, route_cache=cache)
            legs = build_route_legs(make_request(), make_plan())
            with self.assertRaises(AmapProviderError):
                provider.estimate_routes(
                    city="Chengdu",
                    plan_fingerprint="first",
                    legs=legs,
                )

            recovered = provider.estimate_routes(
                city="Chengdu",
                plan_fingerprint="second",
                legs=legs,
            )

        self.assertEqual(RawClient.route_calls, 3)
        self.assertEqual(recovered.cache_hits, 1)
        self.assertEqual(recovered.cache_misses, 1)
        self.assertEqual(len(recovered.routes), 2)



    def test_unavailable_route_uses_short_cache_ttl(self):
        class RecordingCache:
            ttl_seconds = None

            @staticmethod
            def get(cache_key):
                return None

            @classmethod
            def set(cls, cache_key, estimate, *, ttl_seconds):
                cls.ttl_seconds = ttl_seconds

        class RawClient:
            @classmethod
            def district_search(cls, city):
                return {"status": "1", "districts": [{"citycode": "028"}]}

            @classmethod
            def route(cls, **kwargs):
                return {"status": "1", "route": {"transits": []}}

        result = AmapProviderClient(
            raw_client=RawClient,
            route_cache=RecordingCache(),
        ).estimate_routes(
            city="Chengdu",
            plan_fingerprint="fingerprint",
            legs=build_route_legs(make_request(), make_plan())[:1],
        )

        self.assertFalse(result.routes[0].available)
        self.assertEqual(result.routes[0].error_code, "NO_ROUTE")
        self.assertEqual(
            RecordingCache.ttl_seconds,
            settings.AMAP_ROUTE_UNAVAILABLE_CACHE_TTL_SECONDS,
        )



if __name__ == "__main__":
    unittest.main()
