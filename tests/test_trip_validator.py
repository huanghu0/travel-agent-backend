import unittest

from app.schemas.trip_schema import TripPlan, TripRequest
from app.validation import TripPlanValidator, ValidationSeverity


def make_request() -> TripRequest:
    return TripRequest(
        city="成都",
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史"],
    )


def make_plan() -> TripPlan:
    return TripPlan.model_validate(
        {
            "city": "成都市",
            "start_date": "2026-08-10",
            "end_date": "2026-08-11",
            "days": [
                {
                    "date": "2026-08-10",
                    "day_index": 0,
                    "description": "第一天",
                    "transportation": "公共交通",
                    "accommodation": "经济型酒店",
                    "attractions": [],
                    "meals": [],
                },
                {
                    "date": "2026-08-11",
                    "day_index": 1,
                    "description": "第二天",
                    "transportation": "公共交通",
                    "accommodation": "经济型酒店",
                    "attractions": [],
                    "meals": [],
                },
            ],
            "weather_info": [],
            "overall_suggestions": "提前预约。",
            "budget": None,
        }
    )


class TripPlanValidatorTests(unittest.TestCase):
    def setUp(self):
        self.validator = TripPlanValidator()

    def test_warning_only_plan_is_valid(self):
        result = self.validator.validate(make_request(), make_plan())

        self.assertTrue(result.valid)
        self.assertFalse(result.repairable)
        self.assertEqual(result.error_count, 0)
        self.assertGreater(result.warning_count, 0)
        self.assertTrue(
            all(item.severity is ValidationSeverity.WARNING for item in result.issues)
        )

    def test_semantic_and_source_errors_are_structured_and_repairable(self):
        plan_data = make_plan().model_dump()
        plan_data["city"] = "other-city"
        plan_data["days"][1]["date"] = "2026-08-10"
        plan_data["days"][0]["attractions"] = [
            {
                "name": "fictional-attraction",
                "address": "unknown",
                "location": {"longitude": 200, "latitude": 95},
                "visit_duration": 0,
                "description": "test",
            }
        ]
        plan = TripPlan.model_validate(plan_data)

        result = self.validator.validate(
            make_request(),
            plan,
            attractions={"pois": [{"name": "武侯祠"}]},
        )

        codes = {item.code for item in result.issues}
        self.assertFalse(result.valid)
        self.assertTrue(result.repairable)
        self.assertIn("plan.city_mismatch", codes)
        self.assertIn("day.duplicate_date", codes)
        self.assertIn("day.date_sequence_mismatch", codes)
        self.assertIn("attraction.invalid_location", codes)
        self.assertIn("attraction.invalid_visit_duration", codes)
        self.assertIn("attraction.not_in_sources", codes)
        self.assertGreaterEqual(result.error_count, 6)

    def test_standardized_candidate_shape_is_used_for_source_validation(self):
        plan_data = make_plan().model_dump()
        plan_data["days"][0]["attractions"] = [
            {
                "name": "\u6b66\u4faf\u7960",
                "address": "\u6210\u90fd\u5e02\u6b66\u4faf\u7960\u5927\u8857",
                "location": {"longitude": 104.04, "latitude": 30.64},
                "visit_duration": 120,
                "description": "history",
            }
        ]
        result = self.validator.validate(
            make_request(),
            TripPlan.model_validate(plan_data),
            attractions={
                "provider": "amap",
                "candidates": [
                    {
                        "poi_id": "poi-1",
                        "name": "\u6b66\u4faf\u7960",
                        "address": "\u6210\u90fd\u5e02\u6b66\u4faf\u7960\u5927\u8857",
                    }
                ],
            },
        )

        codes = {item.code for item in result.issues}
        self.assertNotIn("attraction.not_in_sources", codes)

    def test_inconsistent_user_request_is_not_llm_repairable(self):
        request = make_request().model_copy(update={"travel_days": 3})

        result = self.validator.validate(request, make_plan())

        self.assertFalse(result.valid)
        self.assertFalse(result.repairable)
        issue = next(
            item for item in result.issues
            if item.code == "request.travel_days_mismatch"
        )
        self.assertFalse(issue.repairable)


    @staticmethod
    def _plan_with_route(*, far=False):
        plan_data = make_plan().model_dump()
        destination_longitude = 105.5 if far else 104.02
        plan_data["days"][0]["attractions"] = [
            {
                "name": "A",
                "address": "address A",
                "location": {"longitude": 104.0, "latitude": 30.0},
                "visit_duration": 60,
                "description": "A",
            },
            {
                "name": "B",
                "address": "address B",
                "location": {
                    "longitude": destination_longitude,
                    "latitude": 30.0,
                },
                "visit_duration": 60,
                "description": "B",
            },
        ]
        return TripPlan.model_validate(plan_data)

    @staticmethod
    def _route_result(*, available=True, distance=5000, duration=900):
        return {
            "provider": "amap",
            "plan_fingerprint": "fingerprint",
            "requested_legs": 1,
            "evaluated_legs": 1,
            "truncated_legs": 0,
            "routes": [
                {
                    "provider": "amap",
                    "day_index": 0,
                    "leg_index": 0,
                    "date": "2026-08-10",
                    "origin_name": "A",
                    "destination_name": "B",
                    "mode": "transit",
                    "available": available,
                    "distance_meters": distance if available else None,
                    "duration_seconds": duration if available else None,
                    "error_code": None if available else "NO_ROUTE",
                    "error_message": None if available else "no route",
                }
            ],
        }

    def test_real_route_duration_over_two_hours_is_repairable_error(self):
        result = self.validator.validate(
            make_request(),
            self._plan_with_route(),
            route_estimates=self._route_result(duration=7201),
        )

        issue = next(item for item in result.issues if item.code == "route.excessive_duration")
        self.assertFalse(result.valid)
        self.assertTrue(result.repairable)
        self.assertEqual(issue.actual["source"], "amap")

    def test_real_route_distance_over_eighty_km_is_warning(self):
        result = self.validator.validate(
            make_request(),
            self._plan_with_route(),
            route_estimates=self._route_result(distance=81000, duration=3600),
        )

        issue = next(item for item in result.issues if item.code == "route.long_transfer")
        self.assertTrue(result.valid)
        self.assertEqual(issue.severity, ValidationSeverity.WARNING)
        self.assertEqual(issue.actual["source"], "amap")

    def test_unavailable_route_warns_and_falls_back_to_haversine(self):
        result = self.validator.validate(
            make_request(),
            self._plan_with_route(far=True),
            route_estimates=self._route_result(available=False),
        )

        codes = {item.code for item in result.issues}
        self.assertTrue(result.valid)
        self.assertIn("route.unavailable", codes)
        self.assertIn("route.long_transfer", codes)
        long_route = next(item for item in result.issues if item.code == "route.long_transfer")
        self.assertEqual(long_route.actual["source"], "haversine")

    def test_missing_route_estimates_keeps_haversine_validation(self):
        result = self.validator.validate(
            make_request(),
            self._plan_with_route(far=True),
        )

        codes = {item.code for item in result.issues}
        self.assertIn("route.long_transfer", codes)
        self.assertNotIn("route.unavailable", codes)



if __name__ == "__main__":
    unittest.main()

