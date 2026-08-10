import unittest

from app.providers.amap.models import (
    RouteEstimate,
    RouteEstimateResult,
    RouteLegRequest,
    RoutePoint,
)
from app.routing import (
    build_route_legs,
    evaluate_route_quality,
    expected_route_leg_keys,
    plan_route_fingerprint,
)
from app.scheduling import ScheduleTimelineEvaluator
from app.schemas.trip_schema import TripPlan, TripRequest


def make_request(days: int = 3) -> TripRequest:
    return TripRequest(
        city="Hangzhou",
        start_date="2026-08-10",
        end_date=f"2026-08-{9 + days:02d}",
        travel_days=days,
        transportation="public transit",
        accommodation="budget hotel",
        preferences=["nature"],
    )


def attraction(name: str, longitude: float, latitude: float, duration: int = 60) -> dict:
    return {
        "name": name,
        "address": f"{name} address",
        "location": {"longitude": longitude, "latitude": latitude},
        "visit_duration": duration,
        "description": name,
    }


def hotel(name: str, longitude: float, latitude: float) -> dict:
    return {
        "name": name,
        "address": f"{name} address",
        "location": {"longitude": longitude, "latitude": latitude},
    }


def make_three_day_plan() -> TripPlan:
    days = []
    for index in range(3):
        day = {
            "date": f"2026-08-{10 + index:02d}",
            "day_index": index,
            "description": f"day {index + 1}",
            "transportation": "public transit",
            "accommodation": "budget hotel",
            "hotel": (
                hotel("Hotel A", 120.10, 30.20)
                if index == 0
                else hotel("Hotel B", 120.20, 30.30)
                if index == 1
                else None
            ),
            "attractions": [
                attraction(f"A{index}", 120.11 + index * 0.1, 30.21 + index * 0.1),
                attraction(f"B{index}", 120.12 + index * 0.1, 30.22 + index * 0.1),
            ],
            "meals": [],
        }
        days.append(day)
    return TripPlan.model_validate(
        {
            "city": "Hangzhou",
            "start_date": "2026-08-10",
            "end_date": "2026-08-12",
            "days": days,
            "weather_info": [],
            "overall_suggestions": "keep time buffers",
        }
    )


class HotelRouteConstructionTests(unittest.TestCase):
    def test_builds_departure_between_and_return_legs_with_checkout_carryover(self):
        request = make_request()
        plan = make_three_day_plan()
        hotel_sources = {
            "candidates": [
                {
                    "poi_id": "hotel-a-id",
                    "name": "Hotel A",
                    "city_code": "0571",
                },
                {
                    "poi_id": "hotel-b-id",
                    "name": "Hotel B",
                    "city_code": "0571",
                },
            ]
        }

        legs = build_route_legs(request, plan, hotels=hotel_sources)

        self.assertEqual(len(legs), 8)
        self.assertEqual(
            [(leg.day_index, leg.leg_type, leg.leg_index) for leg in legs],
            expected_route_leg_keys(plan),
        )
        self.assertEqual(legs[0].origin.poi_id, "hotel-a-id")
        self.assertEqual(legs[2].destination.poi_id, "hotel-a-id")
        day_three = [leg for leg in legs if leg.day_index == 2]
        self.assertEqual([leg.leg_type for leg in day_three], [
            "hotel_departure",
            "between_attractions",
        ])
        self.assertEqual(day_three[0].origin.name, "Hotel B")

    def test_fingerprint_changes_when_hotel_location_changes(self):
        request = make_request()
        plan = make_three_day_plan()
        original = plan_route_fingerprint(request, plan)
        changed = plan.model_copy(deep=True)
        assert changed.days[0].hotel is not None
        assert changed.days[0].hotel.location is not None
        changed.days[0].hotel.location.longitude += 0.01

        self.assertNotEqual(original, plan_route_fingerprint(request, changed))

    def test_old_route_models_default_to_between_attractions(self):
        point = RoutePoint(
            name="A",
            location={"longitude": 120.0, "latitude": 30.0},
        )
        leg = RouteLegRequest(
            day_index=0,
            leg_index=0,
            origin=point,
            destination=point,
            mode="transit",
        )
        estimate = RouteEstimate(
            day_index=0,
            leg_index=0,
            origin_name="A",
            destination_name="B",
            mode="transit",
        )

        self.assertEqual(leg.leg_type, "between_attractions")
        self.assertEqual(estimate.leg_type, "between_attractions")


class HotelTimelineTests(unittest.TestCase):
    def make_one_day_plan(self, *, hotel_longitude: float = 120.10) -> TripPlan:
        return TripPlan.model_validate(
            {
                "city": "Hangzhou",
                "start_date": "2026-08-10",
                "end_date": "2026-08-10",
                "days": [
                    {
                        "date": "2026-08-10",
                        "day_index": 0,
                        "description": "complete hotel timeline",
                        "transportation": "public transit",
                        "accommodation": "budget hotel",
                        "hotel": hotel("Hotel A", hotel_longitude, 30.20),
                        "attractions": [
                            attraction("West Lake", 120.11, 30.21, 30),
                            attraction("Botanical Garden", 120.12, 30.22, 30),
                        ],
                        "meals": [],
                    }
                ],
                "weather_info": [],
                "overall_suggestions": "keep time buffers",
            }
        )

    def test_real_hotel_routes_are_included_in_timeline_and_quality(self):
        request = make_request(days=1)
        plan = self.make_one_day_plan()
        legs = build_route_legs(request, plan)
        routes = RouteEstimateResult(
            plan_fingerprint=plan_route_fingerprint(request, plan),
            requested_legs=len(legs),
            evaluated_legs=len(legs),
            routes=[
                RouteEstimate(
                    day_index=leg.day_index,
                    leg_index=leg.leg_index,
                    leg_type=leg.leg_type,
                    date=leg.date,
                    origin_name=leg.origin.name,
                    destination_name=leg.destination.name,
                    mode=leg.mode,
                    distance_meters=1000,
                    duration_seconds=600,
                )
                for leg in legs
            ],
        )

        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, routes)
        quality = evaluate_route_quality(plan, routes)
        transportation = [
            item for item in schedule.days[0].timeline
            if item.item_type == "transportation"
        ]

        self.assertEqual(len(legs), 3)
        self.assertEqual(schedule.days[0].transportation_minutes, 30)
        self.assertEqual(schedule.days[0].fallback_route_legs, 0)
        self.assertEqual(len(transportation), 3)
        self.assertTrue(transportation[0].name.startswith("Hotel A"))
        self.assertTrue(transportation[-1].name.endswith("Hotel A"))
        self.assertEqual(quality.total_legs, 3)
        self.assertEqual(quality.available_legs, 3)

    def test_missing_far_hotel_routes_use_fallback_and_make_day_infeasible(self):
        request = make_request(days=1)
        plan = self.make_one_day_plan(hotel_longitude=122.50)
        routes = RouteEstimateResult(
            plan_fingerprint=plan_route_fingerprint(request, plan),
            requested_legs=3,
            evaluated_legs=1,
            routes=[
                RouteEstimate(
                    day_index=0,
                    leg_index=0,
                    leg_type="between_attractions",
                    origin_name="West Lake",
                    destination_name="Botanical Garden",
                    mode="transit",
                    distance_meters=1000,
                    duration_seconds=600,
                )
            ],
        )

        report = ScheduleTimelineEvaluator().evaluate(request, plan, routes)

        self.assertEqual(report.days[0].fallback_route_legs, 2)
        self.assertGreater(report.days[0].overtime_minutes, 0)
        self.assertFalse(report.days[0].feasible)


if __name__ == "__main__":
    unittest.main()
