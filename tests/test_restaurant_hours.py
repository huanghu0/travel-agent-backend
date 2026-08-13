import unittest

from app.constraints import ConstraintEvaluator
from app.plan_content import (
    TripPlanConsistencyRebuilder,
    opening_status_for_interval,
    parse_opening_ranges,
)
from app.plan_content.restaurant_hours import meal_service_intervals
from app.providers.amap.models import (
    GeoPoint,
    RestaurantCandidate,
    RestaurantSearchResult,
)
from app.schemas.trip_schema import Meal, TripPlan, TripRequest
from app.scheduling.models import (
    DayScheduleQuality,
    ScheduleQualityReport,
    TimelineItem,
)


def make_request() -> TripRequest:
    return TripRequest(
        city="杭州",
        start_date="2026-08-20",
        end_date="2026-08-20",
        travel_days=1,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["美食"],
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
                    "description": "测试日",
                    "transportation": "公共交通",
                    "accommodation": "测试酒店",
                    "hotel": {
                        "name": "测试酒店",
                        "address": "酒店路1号",
                        "location": {"longitude": 120.15, "latitude": 30.25},
                    },
                    "attractions": [
                        {
                            "name": "西湖",
                            "address": "西湖路1号",
                            "location": {"longitude": 120.14, "latitude": 30.24},
                            "visit_duration": 120,
                            "description": "景点",
                        }
                    ],
                    "meals": [],
                }
            ],
            "weather_info": [],
            "overall_suggestions": "测试",
        }
    )


def make_schedule() -> ScheduleQualityReport:
    return ScheduleQualityReport(
        plan_fingerprint="schedule",
        days=[
            DayScheduleQuality(
                day_index=0,
                timeline=[
                    TimelineItem(
                        item_type="meal",
                        name="午餐",
                        start_time="12:00",
                        end_time="13:00",
                        duration_minutes=60,
                        day_index=0,
                    ),
                    TimelineItem(
                        item_type="transportation",
                        name="返回酒店",
                        start_time="18:20",
                        end_time="18:50",
                        duration_minutes=30,
                        day_index=0,
                    ),
                ],
            )
        ],
    )


def restaurant(
    *,
    poi_id: str,
    name: str,
    opening_hours: str,
    distance: int,
) -> RestaurantCandidate:
    return RestaurantCandidate(
        poi_id=poi_id,
        name=name,
        address=f"{name}地址",
        location=GeoPoint(longitude=120.15, latitude=30.25),
        rating=4.6,
        category="餐饮服务;中餐厅",
        opening_hours=opening_hours,
        distance_meters=distance,
        anchor_id="day-0-lunch",
        day_index=0,
        meal_type="lunch",
    )


class RestaurantOpeningHoursParserTests(unittest.TestCase):
    def test_single_multiple_and_all_day_ranges(self):
        self.assertEqual(parse_opening_ranges("09:00-22:00"), [(540, 1320)])
        self.assertEqual(
            parse_opening_ranges("11:00-14:00;17:00-22:00"),
            [(660, 840), (1020, 1320)],
        )
        self.assertEqual(parse_opening_ranges("00:00-24:00"), [(0, 1440)])
        self.assertEqual(parse_opening_ranges("24小时营业"), [(0, 1440)])

    def test_overnight_and_unknown_ranges(self):
        self.assertEqual(parse_opening_ranges("18:00-02:00"), [(1080, 1560)])
        self.assertEqual(opening_status_for_interval("18:00-02:00", 23 * 60, 24 * 60), "open")
        self.assertEqual(opening_status_for_interval("18:00-02:00", 17 * 60, 18 * 60), "closed")
        self.assertEqual(opening_status_for_interval("暂无", 12 * 60, 13 * 60), "unknown")

    def test_late_timeline_lunch_is_clamped_inside_constraint_window(self):
        schedule_day = {
            "timeline": [
                {
                    "item_type": "meal",
                    "start_time": "13:15",
                    "end_time": "14:15",
                }
            ]
        }

        interval = meal_service_intervals(schedule_day)["lunch"]

        self.assertEqual((interval.start_time, interval.end_time), ("13:00", "14:00"))


class RestaurantOpeningSelectionTests(unittest.TestCase):
    def test_open_restaurant_is_selected_before_nearer_closed_candidate(self):
        restaurants = RestaurantSearchResult(
            query_city="杭州",
            keywords="餐厅",
            candidates=[
                restaurant(
                    poi_id="closed-near",
                    name="近但关闭",
                    opening_hours="08:00-11:00",
                    distance=50,
                ),
                restaurant(
                    poi_id="open-far",
                    name="远但营业",
                    opening_hours="11:00-14:00",
                    distance=400,
                ),
            ],
        )

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            make_request(),
            make_plan(),
            route_estimates=None,
            schedule_quality_report=make_schedule(),
            restaurants=restaurants,
        )
        lunch = next(meal for meal in rebuilt.days[0].meals if meal.type == "lunch")

        self.assertEqual(lunch.name, "远但营业")
        self.assertEqual(lunch.opening_status, "open")
        self.assertEqual(lunch.planned_start_time, "12:00")
        self.assertEqual(lunch.planned_end_time, "13:00")
        dinner = next(meal for meal in rebuilt.days[0].meals if meal.type == "dinner")
        self.assertEqual(dinner.planned_start_time, "18:50")

    def test_all_closed_candidates_fall_back(self):
        restaurants = RestaurantSearchResult(
            query_city="杭州",
            keywords="餐厅",
            candidates=[
                restaurant(
                    poi_id="closed",
                    name="午间关闭",
                    opening_hours="17:00-22:00",
                    distance=20,
                )
            ],
        )

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            make_request(),
            make_plan(),
            route_estimates=None,
            schedule_quality_report=make_schedule(),
            restaurants=restaurants,
        )
        lunch = next(meal for meal in rebuilt.days[0].meals if meal.type == "lunch")

        self.assertEqual(lunch.source, "fallback")
        self.assertEqual(lunch.opening_status, "fallback")

    def test_unknown_hours_are_allowed_as_deterministic_degradation(self):
        restaurants = RestaurantSearchResult(
            query_city="杭州",
            keywords="餐厅",
            candidates=[
                restaurant(
                    poi_id="unknown",
                    name="营业时间未知",
                    opening_hours="",
                    distance=80,
                )
            ],
        )

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            make_request(),
            make_plan(),
            route_estimates=None,
            schedule_quality_report=make_schedule(),
            restaurants=restaurants,
        )
        lunch = next(meal for meal in rebuilt.days[0].meals if meal.type == "lunch")

        self.assertEqual(lunch.source, "amap")
        self.assertEqual(lunch.opening_status, "unknown")


class RestaurantOpeningConstraintTests(unittest.TestCase):
    def test_closed_restaurant_is_reported_as_repairable_error(self):
        plan = make_plan()
        plan.days[0].meals = [
            Meal(
                type="lunch",
                name="晚市餐厅",
                opening_hours="17:00-22:00",
                planned_start_time="12:00",
                planned_end_time="13:00",
                source="amap",
            )
        ]
        plan = TripPlan.model_validate(plan.model_dump(mode="json"))

        report = ConstraintEvaluator().evaluate(
            make_request(),
            plan,
            make_schedule(),
        )
        issue = next(item for item in report.issues if item.code == "meal.outside_opening_hours")

        self.assertTrue(issue.repairable)
        self.assertEqual(issue.path, "days[0].meals[0]")
        self.assertEqual(issue.expected, ["17:00-22:00"])


if __name__ == "__main__":
    unittest.main()
