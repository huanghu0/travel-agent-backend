import unittest
from unittest.mock import patch

from app.agent_runtime import AgentState
from app.plan_content import (
    TripPlanConsistencyRebuilder,
    build_restaurant_search_anchors,
    restaurant_search_source_fingerprint,
)
from app.providers.amap.client import AmapClient, AmapProviderClient
from app.providers.amap.models import (
    AttractionSearchResult,
    GeoPoint,
    HotelSearchResult,
    LocationResolutionResult,
    PoiCandidate,
    PoiDetailResult,
    PoiSearchResult,
    RestaurantCandidate,
    RestaurantSearchAnchor,
    RestaurantSearchResult,
    WeatherSearchResult,
)
from app.providers.amap.normalizers import (
    normalize_geocode_location,
    normalize_poi_detail,
    normalize_pois,
    normalize_restaurants,
)
from app.schemas.trip_schema import TripPlan, TripRequest
from app.tools import build_trip_tool_registry


def make_request() -> TripRequest:
    return TripRequest(
        city="杭州",
        start_date="2026-08-20",
        end_date="2026-08-20",
        travel_days=1,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["自然风光"],
    )


def make_plan() -> TripPlan:
    return TripPlan.model_validate(
        {
            "city": "杭州",
            "start_date": "2026-08-20",
            "end_date": "2026-08-20",
            "days": [
                {
                    "date": "2026-08-20",
                    "day_index": 0,
                    "description": "旧描述",
                    "transportation": "公共交通",
                    "accommodation": "旧住宿描述",
                    "hotel": {
                        "name": "西湖酒店",
                        "address": "杭州市西湖区酒店路1号",
                        "location": {"longitude": 120.1500, "latitude": 30.2500},
                        "estimated_cost": 260,
                    },
                    "attractions": [
                        {
                            "name": "西湖风景区",
                            "address": "杭州市西湖区龙井路1号",
                            "location": {"longitude": 120.1400, "latitude": 30.2400},
                            "visit_duration": 180,
                            "description": "自然景观",
                            "poi_id": "attraction-1",
                            "ticket_price": 0,
                        }
                    ],
                    "meals": [],
                }
            ],
            "weather_info": [],
            "overall_suggestions": "旧建议",
        }
    )


def raw_poi(
    poi_id: str,
    name: str,
    *,
    distance: str = "300",
    rating: str = "4.5",
    cost: str = "60",
) -> dict:
    return {
        "id": poi_id,
        "name": name,
        "address": f"{name}地址",
        "location": "120.150000,30.250000",
        "type": "餐饮服务;中餐厅",
        "typecode": "050100",
        "distance": distance,
        "tel": "0571-12345678",
        "business": {
            "rating": rating,
            "cost": cost,
            "opentime_today": "10:00-22:00",
        },
    }


class AmapPoiV5TransportTests(unittest.TestCase):
    def test_text_search_detail_and_geocode_use_expected_endpoints(self):
        calls = []

        def fake_get_json(url, params):
            calls.append((url, params))
            return {"status": "1", "info": "OK"}

        with patch.object(AmapClient, "_get_json", side_effect=fake_get_json):
            AmapClient.text_search(
                keywords="??",
                city="??",
                types="110000",
                page=2,
                page_size=30,
            )
            AmapClient.poi_detail("poi-1")
            AmapClient.geocode(address="龙井路1号", city="杭州")

        text_url, text_params = calls[0]
        self.assertTrue(text_url.endswith("/v5/place/text"))
        self.assertEqual(text_params["region"], "??")
        self.assertEqual(text_params["types"], "110000")
        self.assertEqual(text_params["show_fields"], "business")
        self.assertEqual(text_params["page_size"], 25)
        self.assertEqual(text_params["page_num"], 2)
        self.assertTrue(calls[1][0].endswith("/v5/place/detail"))
        self.assertTrue(calls[2][0].endswith("/v3/geocode/geo"))


class AmapLocationDiningNormalizerTests(unittest.TestCase):
    def test_v5_business_fields_and_address_fallback_are_normalized(self):
        payload = {
            "pois": [
                raw_poi("food-1", "湖畔餐厅"),
                {
                    "id": "station-1",
                    "name": "杭州东站",
                    "address": [],
                    "adname": "上城区",
                    "location": "120.212,30.290",
                    "business": {"rating": "4.2"},
                },
            ]
        }

        result = normalize_pois(
            payload,
            city="杭州",
            keywords="餐厅",
            types="",
            limit=10,
        )

        restaurant = next(item for item in result.candidates if item.poi_id == "food-1")
        station = next(item for item in result.candidates if item.poi_id == "station-1")
        self.assertEqual(restaurant.rating, 4.5)
        self.assertEqual(restaurant.average_cost, 60)
        self.assertEqual(restaurant.opening_hours, "10:00-22:00")
        self.assertEqual(station.address, "上城区")

    def test_restaurants_sort_by_distance_before_rating(self):
        anchor = RestaurantSearchAnchor(
            anchor_id="day-0-lunch",
            day_index=0,
            meal_type="lunch",
            name="西湖风景区",
            location=GeoPoint(longitude=120.14, latitude=30.24),
        )
        payload = {
            "pois": [
                raw_poi("far", "远处高分餐厅", distance="800", rating="5.0"),
                raw_poi("near", "附近餐厅", distance="120", rating="4.0"),
            ]
        }

        result = normalize_restaurants(
            payload,
            city="杭州",
            keywords="餐厅",
            anchor=anchor,
            limit=2,
        )

        self.assertEqual([item.poi_id for item in result.candidates], ["near", "far"])
        self.assertTrue(all(item.anchor_id == "day-0-lunch" for item in result.candidates))

    def test_poi_detail_and_geocode_normalizers_cover_found_and_missing(self):
        found = normalize_poi_detail(
            {"pois": [raw_poi("food-1", "湖畔餐厅")]},
            poi_id="food-1",
        )
        missing = normalize_poi_detail({"pois": []}, poi_id="missing")
        geocode = normalize_geocode_location(
            {
                "geocodes": [
                    {
                        "formatted_address": "浙江省杭州市西湖区龙井路1号",
                        "location": "120.140000,30.240000",
                        "district": "西湖区",
                        "citycode": "0571",
                        "adcode": "330106",
                    }
                ]
            },
            query="龙井路1号",
            city="杭州",
        )

        self.assertTrue(found.found)
        self.assertEqual(found.candidate.poi_id, "food-1")
        self.assertFalse(missing.found)
        self.assertTrue(geocode.resolved)
        self.assertEqual(geocode.source, "geocode")
        self.assertEqual(geocode.candidate.location.longitude, 120.14)


class AmapLocationDiningProviderTests(unittest.TestCase):
    def test_same_coordinate_reuses_http_result_and_binds_each_meal(self):
        class RawClient:
            calls = []

            @classmethod
            def around_search(cls, **kwargs):
                cls.calls.append(kwargs)
                return {
                    "status": "1",
                    "info": "OK",
                    "pois": [raw_poi("food-1", "湖畔餐厅")],
                }

        provider = AmapProviderClient(raw_client=RawClient)
        same_location = GeoPoint(longitude=120.15, latitude=30.25)
        anchors = [
            RestaurantSearchAnchor(
                anchor_id="day-0-breakfast",
                day_index=0,
                meal_type="breakfast",
                name="西湖酒店",
                location=same_location,
            ),
            RestaurantSearchAnchor(
                anchor_id="day-0-dinner",
                day_index=0,
                meal_type="dinner",
                name="西湖酒店",
                location=same_location,
            ),
        ]

        result = provider.search_restaurants(
            city="杭州",
            anchors=anchors,
            max_anchors=8,
            candidates_per_anchor=2,
        )

        self.assertEqual(len(RawClient.calls), 1)
        self.assertEqual(result.searched_anchors, 1)
        self.assertEqual(
            [(item.anchor_id, item.meal_type) for item in result.candidates],
            [("day-0-breakfast", "breakfast"), ("day-0-dinner", "dinner")],
        )
        self.assertEqual(RawClient.calls[0]["types"], "050000")

    def test_restaurant_anchor_and_candidate_limits_are_enforced(self):
        class RawClient:
            calls = []

            @classmethod
            def around_search(cls, **kwargs):
                cls.calls.append(kwargs)
                return {
                    "status": "1",
                    "info": "OK",
                    "pois": [
                        raw_poi("one", "餐厅一", distance="100"),
                        raw_poi("two", "餐厅二", distance="200"),
                    ],
                }

        provider = AmapProviderClient(raw_client=RawClient)
        anchors = [
            RestaurantSearchAnchor(
                anchor_id=f"day-{index}-lunch",
                day_index=index,
                meal_type="lunch",
                name=f"景点{index}",
                location=GeoPoint(longitude=120.15 + index / 100, latitude=30.25),
            )
            for index in range(3)
        ]

        result = provider.search_restaurants(
            city="杭州",
            anchors=anchors,
            max_anchors=2,
            candidates_per_anchor=1,
        )

        self.assertEqual(len(RawClient.calls), 2)
        self.assertEqual(result.truncated_anchors, 1)
        self.assertEqual(len(result.candidates), 2)
        self.assertTrue(all(call["page_size"] == 1 for call in RawClient.calls))

    def test_resolve_location_prefers_exact_poi_then_falls_back_to_geocode(self):
        class PoiRawClient:
            @staticmethod
            def text_search(**kwargs):
                return {
                    "status": "1",
                    "info": "OK",
                    "pois": [
                        raw_poi("other", "西湖风景区南门"),
                        raw_poi("exact", "西湖风景区"),
                    ],
                }

            @staticmethod
            def geocode(**kwargs):
                raise AssertionError("POI 精确匹配时不应调用地理编码")

        exact = AmapProviderClient(raw_client=PoiRawClient).resolve_location(
            query="西湖风景区",
            city="杭州",
        )
        self.assertEqual(exact.source, "poi")
        self.assertEqual(exact.confidence, 0.98)
        self.assertEqual(exact.candidate.poi_id, "exact")

        class GeocodeRawClient:
            @staticmethod
            def text_search(**kwargs):
                return {"status": "1", "info": "OK", "pois": []}

            @staticmethod
            def geocode(**kwargs):
                return {
                    "status": "1",
                    "info": "OK",
                    "geocodes": [
                        {
                            "formatted_address": "浙江省杭州市西湖区龙井路1号",
                            "location": "120.140000,30.240000",
                        }
                    ],
                }

        fallback = AmapProviderClient(raw_client=GeocodeRawClient).resolve_location(
            query="龙井路1号",
            city="杭州",
        )
        self.assertEqual(fallback.source, "geocode")
        self.assertEqual(fallback.confidence, 0.72)


class LocationDiningToolRegistryTests(unittest.TestCase):
    def test_four_location_tools_use_standardized_provider(self):
        class StandardProvider:
            def search_attractions(self, *, city, keywords):
                return AttractionSearchResult(query_city=city, keywords=keywords)

            def search_hotels(self, *, city, keywords):
                return HotelSearchResult(query_city=city, keywords=keywords)

            def get_weather(self, city):
                return WeatherSearchResult(query_city=city)

            def search_pois(self, **kwargs):
                return PoiSearchResult(
                    query_city=kwargs["city"],
                    keywords=kwargs["keywords"],
                    types=kwargs["types"],
                )

            def search_restaurants(self, **kwargs):
                return RestaurantSearchResult(
                    query_city=kwargs["city"],
                    keywords=kwargs["keywords"],
                    requested_anchors=len(kwargs["anchors"]),
                )

            def get_poi_detail(self, poi_id):
                return PoiDetailResult(poi_id=poi_id)

            def resolve_location(self, *, query, city):
                return LocationResolutionResult(query=query, city=city)

        registry = build_trip_tool_registry(
            planner_agent=object(),
            map_provider=StandardProvider(),
        )
        anchor = {
            "anchor_id": "day-0-lunch",
            "day_index": 0,
            "meal_type": "lunch",
            "name": "西湖风景区",
            "location": {"longitude": 120.14, "latitude": 30.24},
        }

        poi = registry.execute("search_pois", {"city": "杭州", "keywords": "医院"})
        dining = registry.execute(
            "search_restaurants",
            {"city": "杭州", "anchors": [anchor]},
        )
        detail = registry.execute("get_poi_detail", {"poi_id": "poi-1"})
        resolved = registry.execute(
            "resolve_location",
            {"query": "杭州东站", "city": "杭州"},
        )

        self.assertTrue(all(item.success for item in (poi, dining, detail, resolved)))
        self.assertEqual(dining.data["requested_anchors"], 1)
        self.assertEqual(detail.data["poi_id"], "poi-1")
        self.assertEqual(resolved.data["source"], "none")

    def test_legacy_standardized_provider_returns_empty_compatible_results(self):
        class LegacyProvider:
            def search_attractions(self, *, city, keywords):
                return AttractionSearchResult(query_city=city, keywords=keywords)

            def search_hotels(self, *, city, keywords):
                return HotelSearchResult(query_city=city, keywords=keywords)

            def get_weather(self, city):
                return WeatherSearchResult(query_city=city)

        registry = build_trip_tool_registry(
            planner_agent=object(),
            map_provider=LegacyProvider(),
        )

        self.assertTrue(
            registry.execute("search_pois", {"keywords": "医院"}).success
        )
        self.assertTrue(
            registry.execute(
                "search_restaurants",
                {"city": "杭州", "anchors": []},
            ).success
        )
        self.assertFalse(
            registry.execute("get_poi_detail", {"poi_id": "missing"}).data["found"]
        )
        self.assertFalse(
            registry.execute(
                "resolve_location",
                {"query": "未知地点", "city": "杭州"},
            ).data["resolved"]
        )


class DiningClosureTests(unittest.TestCase):
    def test_anchor_builder_is_bounded_and_fingerprint_changes_with_location(self):
        plan = make_plan()
        anchors = build_restaurant_search_anchors(plan, max_anchors=2)
        before = restaurant_search_source_fingerprint(plan, max_anchors=8)
        changed = plan.model_copy(deep=True)
        changed.days[0].attractions[0].location.longitude += 0.01
        after = restaurant_search_source_fingerprint(changed, max_anchors=8)

        self.assertEqual(len(anchors), 2)
        self.assertEqual(anchors[0].meal_type, "lunch")
        self.assertNotEqual(before, after)

    def test_rebuilder_uses_real_restaurant_and_falls_back_without_candidate(self):
        plan = make_plan()
        restaurant = RestaurantCandidate(
            poi_id="food-1",
            name="湖畔餐厅",
            address="杭州市西湖区湖滨路1号",
            location=GeoPoint(longitude=120.141, latitude=30.241),
            rating=4.8,
            telephone="0571-12345678",
            category="餐饮服务;中餐厅",
            opening_hours="10:00-22:00",
            average_cost=88,
            distance_meters=180,
            anchor_id="day-0-lunch",
            day_index=0,
            meal_type="lunch",
        )
        restaurants = RestaurantSearchResult(
            query_city="杭州",
            keywords="餐厅",
            requested_anchors=1,
            searched_anchors=1,
            candidates=[restaurant],
        )

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            make_request(),
            plan,
            route_estimates=None,
            restaurants=restaurants,
        )
        meals = {meal.type: meal for meal in rebuilt.days[0].meals}

        self.assertEqual(meals["lunch"].name, "湖畔餐厅")
        self.assertEqual(meals["lunch"].source, "amap")
        self.assertEqual(meals["lunch"].estimated_cost, 88)
        self.assertEqual(meals["breakfast"].source, "fallback")
        self.assertEqual(meals["dinner"].source, "fallback")

    def test_rebuilder_does_not_repeat_same_restaurant_in_one_day(self):
        plan = make_plan()
        shared = {
            "poi_id": "food-shared",
            "name": "同一家餐厅",
            "address": "杭州市西湖区共享路1号",
            "location": GeoPoint(longitude=120.15, latitude=30.25),
            "average_cost": 50,
            "distance_meters": 100,
            "day_index": 0,
        }
        restaurants = RestaurantSearchResult(
            query_city="杭州",
            keywords="餐厅",
            candidates=[
                RestaurantCandidate(
                    **shared,
                    anchor_id="day-0-breakfast",
                    meal_type="breakfast",
                ),
                RestaurantCandidate(
                    **shared,
                    anchor_id="day-0-lunch",
                    meal_type="lunch",
                ),
            ],
        )

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            make_request(),
            plan,
            route_estimates=None,
            restaurants=restaurants,
        )
        meals = rebuilt.days[0].meals

        self.assertEqual(sum(meal.source == "amap" for meal in meals), 1)
        self.assertEqual(sum(meal.name == "同一家餐厅" for meal in meals), 1)

    def test_old_agent_state_without_restaurant_fields_can_be_restored(self):
        payload = AgentState.create(make_request()).model_dump(mode="json")
        payload["state_version"] = 15
        payload.pop("restaurants")
        payload.pop("restaurant_plan_fingerprint")

        restored = AgentState.model_validate(payload)

        self.assertIsNone(restored.restaurants)
        self.assertIsNone(restored.restaurant_plan_fingerprint)


if __name__ == "__main__":
    unittest.main()

