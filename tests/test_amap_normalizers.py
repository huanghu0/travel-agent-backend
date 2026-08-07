import unittest

from app.providers.amap.models import RouteLegRequest
from app.providers.amap.normalizers import (
    normalize_attractions,
    normalize_city_code,
    normalize_hotels,
    normalize_route,
    normalize_weather,
)


class AmapNormalizerTests(unittest.TestCase):
    def test_attractions_filter_dedupe_sort_and_crop(self):
        payload = {
            "status": "1",
            "pois": [
                {
                    "id": "low",
                    "name": "低评分景点",
                    "address": "成都一街",
                    "location": "104.10,30.60",
                    "type": "风景名胜",
                    "biz_ext": {"rating": "3.8"},
                },
                {
                    "id": "high",
                    "name": "高评分景点",
                    "address": "成都二街",
                    "location": "104.20,30.70",
                    "type": "博物馆",
                    "biz_ext": {"rating": "4.9"},
                },
                {
                    "id": "high",
                    "name": "高评分景点重复项",
                    "address": "成都二街",
                    "location": "104.20,30.70",
                    "type": "博物馆",
                    "biz_ext": {"rating": "5.0", "opentime": "09:00-17:00"},
                },
                {
                    "id": "missing-address",
                    "name": "无地址景点",
                    "address": [],
                    "location": "104.30,30.80",
                },
                {
                    "id": "invalid-location",
                    "name": "错误坐标景点",
                    "address": "成都三街",
                    "location": "999,999",
                },
            ],
        }

        result = normalize_attractions(
            payload,
            city="成都",
            keywords="历史",
            limit=2,
        )

        self.assertEqual(result.total_received, 5)
        self.assertEqual([item.poi_id for item in result.candidates], ["high", "low"])
        self.assertEqual(result.candidates[0].location.longitude, 104.2)
        self.assertEqual(result.candidates[0].rating, 5.0)
        self.assertEqual(result.candidates[0].name, "高评分景点重复项")
        self.assertEqual(result.candidates[0].category, "博物馆")

    def test_hotels_normalize_empty_values_and_negative_cost(self):
        payload = {
            "pois": [
                {
                    "id": "hotel-1",
                    "name": "测试酒店",
                    "address": "人民路 1 号",
                    "location": {"lng": "104.1", "lat": "30.6"},
                    "type": "住宿服务;宾馆酒店",
                    "tel": ["028-1", "028-2"],
                    "biz_ext": {"rating": "4.6", "cost": "-1"},
                }
            ]
        }

        result = normalize_hotels(payload, city="成都", keywords="酒店", limit=5)

        self.assertEqual(len(result.candidates), 1)
        hotel = result.candidates[0]
        self.assertEqual(hotel.type, "住宿服务;宾馆酒店")
        self.assertEqual(hotel.telephone, "028-1,028-2")
        self.assertIsNone(hotel.estimated_cost)

    def test_weather_flattens_sorts_deduplicates_and_crops(self):
        payload = {
            "forecasts": [
                {
                    "city": "成都市",
                    "province": "四川",
                    "reporttime": "2026-08-06 10:00:00",
                    "casts": [
                        {
                            "date": "2026-08-08",
                            "dayweather": "晴",
                            "nightweather": "多云",
                            "daytemp": "32",
                            "nighttemp": "24",
                            "daywind": "南",
                            "daypower": "≤3",
                        },
                        {
                            "date": "invalid",
                            "dayweather": "无效",
                        },
                        {
                            "date": "2026-08-07",
                            "dayweather": "雨",
                            "nightweather": "雨",
                            "daytemp_float": "28.5",
                            "nighttemp_float": "22.0",
                        },
                        {
                            "date": "2026-08-07",
                            "dayweather": "重复",
                        },
                    ],
                }
            ]
        }

        result = normalize_weather(payload, city="成都", limit=1)

        self.assertEqual(result.city, "成都市")
        self.assertEqual(len(result.forecasts), 1)
        self.assertEqual(result.forecasts[0].date, "2026-08-07")
        self.assertEqual(result.forecasts[0].day_temp, 28.5)


    @staticmethod
    def _route_leg(mode="driving"):
        return RouteLegRequest.model_validate(
            {
                "day_index": 0,
                "leg_index": 0,
                "date": "2026-08-10",
                "origin": {
                    "name": "A",
                    "location": {"longitude": 104.01, "latitude": 30.61},
                },
                "destination": {
                    "name": "B",
                    "location": {"longitude": 104.02, "latitude": 30.62},
                },
                "mode": mode,
            }
        )

    def test_poi_city_codes_are_preserved_for_route_queries(self):
        payload = {
            "pois": [
                {
                    "id": "poi-1",
                    "name": "A",
                    "address": "address A",
                    "location": "104.01,30.61",
                    "citycode": "028",
                    "adcode": "510107",
                }
            ]
        }

        result = normalize_attractions(payload, city="Chengdu", keywords="A", limit=5)

        self.assertEqual(result.candidates[0].city_code, "028")
        self.assertEqual(result.candidates[0].adcode, "510107")

    def test_city_code_and_route_metrics_are_normalized(self):
        self.assertEqual(
            normalize_city_code({"districts": [{"citycode": "028"}]}),
            "028",
        )
        for mode, collection in (("driving", "paths"), ("walking", "paths"), ("transit", "transits")):
            with self.subTest(mode=mode):
                route = normalize_route(
                    {
                        "route": {
                            collection: [
                                {"distance": "1234", "cost": {"duration": "900"}}
                            ]
                        }
                    },
                    leg=self._route_leg(mode),
                    mode=mode,
                )
                self.assertTrue(route.available)
                self.assertEqual(route.distance_meters, 1234)
                self.assertEqual(route.duration_seconds, 900)

    def test_missing_route_is_returned_as_unavailable(self):
        route = normalize_route(
            {"route": {"paths": []}},
            leg=self._route_leg("driving"),
            mode="driving",
        )

        self.assertFalse(route.available)
        self.assertEqual(route.error_code, "NO_ROUTE")



if __name__ == "__main__":
    unittest.main()

