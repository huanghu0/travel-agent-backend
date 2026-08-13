import unittest

from app.agent_runtime import AgentAction, AgentState, TripOrchestrator
from app.constraints import ConstraintEvaluator, constraint_plan_fingerprint
from app.plan_content import (
    MinimumAttractionRefillOptimizer,
    TripPlanConsistencyRebuilder,
    attraction_identity,
    count_attractions,
)
from app.providers.amap.models import RouteEstimate, RouteEstimateResult
from app.routing import evaluate_route_quality, plan_route_fingerprint
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleTimelineEvaluator
from app.tools.registry import ToolRegistry
from app.validation import TripPlanValidator


def make_request(*, days: int = 3, transportation: str = "transit") -> TripRequest:
    return TripRequest(
        city="Hangzhou",
        start_date="2026-08-10",
        end_date=f"2026-08-{9 + days:02d}",
        travel_days=days,
        transportation=transportation,
        accommodation="budget hotel",
        preferences=["nature"],
    )


def attraction(
    name: str,
    longitude: float,
    *,
    latitude: float = 30.25,
    poi_id: str | None = None,
    duration: int = 90,
    ticket: int = 0,
) -> dict:
    return {
        "name": name,
        "address": f"{name} address",
        "location": {"longitude": longitude, "latitude": latitude},
        "visit_duration": duration,
        "description": f"{name} description",
        "category": "nature",
        "rating": 4.5,
        "poi_id": poi_id or name.lower(),
        "ticket_price": ticket,
    }


def hotel(name: str = "Lake Hotel", longitude: float = 120.15) -> dict:
    return {
        "name": name,
        "address": f"{name} address",
        "location": {"longitude": longitude, "latitude": 30.25},
        "estimated_cost": 200,
    }


def make_plan(
    *,
    days: int = 3,
    attractions_by_day: list[list[dict]] | None = None,
    include_hotel: bool = True,
    transportation: str = "transit",
) -> TripPlan:
    attractions_by_day = attractions_by_day or [[] for _ in range(days)]
    return TripPlan.model_validate(
        {
            "city": "Hangzhou",
            "start_date": "2026-08-10",
            "end_date": f"2026-08-{9 + days:02d}",
            "days": [
                {
                    "date": f"2026-08-{10 + index:02d}",
                    "day_index": index,
                    "description": "Deleted Place appears in stale text",
                    "transportation": transportation,
                    "accommodation": "stale accommodation",
                    "attractions": day_attractions,
                    "meals": [
                        {
                            "type": "lunch",
                            "name": "Deleted Place lunch",
                            "description": "stale meal",
                            "estimated_cost": 999,
                        }
                    ],
                    "hotel": hotel() if include_hotel else None,
                }
                for index, day_attractions in enumerate(attractions_by_day)
            ],
            "weather_info": [],
            "overall_suggestions": "Visit Deleted Place",
            "budget": {
                "total_attractions": 999,
                "total_hotels": 999,
                "total_meals": 999,
                "total_transportation": 999,
                "total": 3996,
            },
        }
    )


def candidates(*items: dict) -> dict:
    return {
        "provider": "amap",
        "query_city": "Hangzhou",
        "keywords": "nature",
        "total_received": len(items),
        "candidates": [
            {
                "poi_id": item.get("poi_id", item["name"].lower()),
                "name": item["name"],
                "address": item.get("address", f"{item['name']} address"),
                "location": {
                    "longitude": item["longitude"],
                    "latitude": item.get("latitude", 30.25),
                },
                "district": item.get("district", "Xihu"),
                "category": item.get("category", "nature"),
                "rating": item.get("rating", 4.5),
            }
            for item in items
        ],
    }


def empty_routes(request: TripRequest, plan: TripPlan) -> RouteEstimateResult:
    fingerprint = plan_route_fingerprint(request, plan)
    return RouteEstimateResult(
        plan_fingerprint=fingerprint,
        requested_legs=0,
        evaluated_legs=0,
        routes=[],
    )


class MinimumAttractionRefillOptimizerTests(unittest.TestCase):
    def test_required_total_is_one_for_one_day_and_two_for_longer_trip(self):
        optimizer = MinimumAttractionRefillOptimizer(minimum_total_attractions=2)

        self.assertEqual(optimizer.required_total(make_request(days=1), make_plan(days=1)), 1)
        self.assertEqual(optimizer.required_total(make_request(days=3), make_plan(days=3)), 2)

    def test_used_and_rejected_candidates_are_excluded(self):
        request = make_request(days=2)
        plan = make_plan(
            days=2,
            attractions_by_day=[[attraction("Used", 120.15, poi_id="used")], []],
        )
        optimizer = MinimumAttractionRefillOptimizer(minimum_total_attractions=2)
        source = candidates(
            {"name": "Used", "poi_id": "used", "longitude": 120.151},
            {"name": "Rejected", "poi_id": "rejected", "longitude": 120.152},
            {"name": "Available", "poi_id": "available", "longitude": 120.153},
        )

        result = optimizer.optimize(
            request,
            plan,
            attractions=source,
            excluded_candidate_identities={
                attraction_identity(poi_id="rejected", name="Rejected")
            },
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.added_attraction_names, ["Available"])
        self.assertEqual(count_attractions(result.plan), 2)

    def test_nearest_candidate_is_selected_without_mutating_original_plan(self):
        request = make_request(days=1)
        plan = make_plan(days=1)
        before = plan.model_dump(mode="json")
        optimizer = MinimumAttractionRefillOptimizer(minimum_total_attractions=2)

        result = optimizer.optimize(
            request,
            plan,
            attractions=candidates(
                {"name": "Far", "longitude": 121.20, "rating": 5.0},
                {"name": "Near", "longitude": 120.151, "rating": 4.0},
            ),
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.added_attraction_names, ["Near"])
        self.assertEqual(plan.model_dump(mode="json"), before)

    def test_candidate_is_rejected_when_approximate_timeline_is_infeasible(self):
        evaluator = ScheduleTimelineEvaluator(
            default_start_time="09:00",
            default_end_time="10:00",
            lunch_duration_minutes=0,
            route_buffer_minutes=0,
            attraction_buffer_minutes=0,
        )
        optimizer = MinimumAttractionRefillOptimizer(
            evaluator=evaluator,
            minimum_total_attractions=1,
            default_visit_duration_minutes=120,
        )

        result = optimizer.optimize(
            make_request(days=1),
            make_plan(days=1, include_hotel=False),
            attractions=candidates({"name": "Too Long", "longitude": 120.15}),
        )

        self.assertIsNone(result)


class TripPlanConsistencyRebuilderTests(unittest.TestCase):
    def test_rebuild_removes_stale_place_references_and_recalculates_budget(self):
        request = make_request(days=1)
        plan = make_plan(
            days=1,
            attractions_by_day=[[attraction("Kept Place", 120.16, ticket=50)]],
        )
        fingerprint = plan_route_fingerprint(request, plan)
        routes = RouteEstimateResult(
            plan_fingerprint=fingerprint,
            requested_legs=2,
            evaluated_legs=2,
            routes=[
                RouteEstimate(
                    day_index=0,
                    leg_index=0,
                    leg_type="hotel_departure",
                    origin_name="Lake Hotel",
                    destination_name="Kept Place",
                    mode="transit",
                    distance_meters=5000,
                    duration_seconds=1200,
                ),
                RouteEstimate(
                    day_index=0,
                    leg_index=0,
                    leg_type="hotel_return",
                    origin_name="Kept Place",
                    destination_name="Lake Hotel",
                    mode="transit",
                    distance_meters=5000,
                    duration_seconds=1200,
                ),
            ],
        )

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            request,
            plan,
            route_estimates=routes,
        )
        rendered = rebuilt.model_dump_json()

        self.assertNotIn("Deleted Place", rendered)
        self.assertIn("Kept Place", rebuilt.days[0].description)
        self.assertEqual(len(rebuilt.days[0].meals), 3)
        self.assertEqual(rebuilt.budget.total_attractions, 50)
        self.assertEqual(rebuilt.budget.total_hotels, 200)
        self.assertEqual(rebuilt.budget.total_meals, 145)
        self.assertEqual(rebuilt.budget.total_transportation, 10)
        self.assertEqual(rebuilt.budget.total, 405)

    def test_rebuild_preserves_route_fingerprint_when_day_mode_differs_from_request(self):
        request = make_request(days=1, transportation="transit")
        plan = make_plan(
            days=1,
            attractions_by_day=[[attraction("Kept Place", 120.16)]],
            transportation="driving",
        )
        before = plan_route_fingerprint(request, plan)
        routes = empty_routes(request, plan)

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            request,
            plan,
            route_estimates=routes,
        )

        self.assertEqual(plan_route_fingerprint(request, rebuilt), before)
        self.assertIn("\u9a7e\u8f66", rebuilt.days[0].transportation)


    def test_rebuild_preserves_fingerprint_with_mixed_segment_mode_description(self):
        request = make_request(days=1, transportation="driving")
        plan = make_plan(
            days=1,
            attractions_by_day=[[attraction("Kept Place", 120.16)]],
            transportation="driving",
        )
        before = plan_route_fingerprint(request, plan)
        routes = RouteEstimateResult(
            plan_fingerprint=before,
            requested_legs=1,
            evaluated_legs=1,
            routes=[
                RouteEstimate(
                    day_index=0,
                    leg_index=0,
                    leg_type="hotel_departure",
                    origin_name="Lake Hotel",
                    destination_name="Kept Place",
                    # 模拟供应商降级返回的公交分段，不应改写整日主交通方式。
                    mode="transit",
                    distance_meters=5000,
                    duration_seconds=1200,
                )
            ],
        )

        rebuilt = TripPlanConsistencyRebuilder().rebuild(
            request,
            plan,
            route_estimates=routes,
        )

        self.assertIn("\u51fa\u884c\u65b9\u5f0f\uff1a\u9a7e\u8f66", rebuilt.days[0].transportation)
        self.assertIn("\u516c\u5171\u4ea4\u901a\u7ea6", rebuilt.days[0].transportation)
        self.assertEqual(plan_route_fingerprint(request, rebuilt), before)


class ContentRefillOrchestratorTests(unittest.TestCase):
    def make_ready_state(self) -> AgentState:
        request = make_request(days=1)
        plan = make_plan(days=1, include_hotel=False)
        routes = empty_routes(request, plan)
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, routes)
        constraints = ConstraintEvaluator().evaluate(
            request,
            plan,
            schedule,
            attractions=candidates({"name": "Near", "longitude": 120.151}),
            weather={"forecasts": []},
        )
        state = AgentState.create(
            request,
            minimum_total_attractions=1,
            max_content_refill_attempts=2,
        )
        state.attractions = candidates({"name": "Near", "longitude": 120.151})
        state.weather = {"forecasts": []}
        state.hotels = {"candidates": []}
        state.trip_plan = plan
        state.route_estimates = routes.model_dump(mode="json")
        state.route_plan_fingerprint = routes.plan_fingerprint
        state.route_quality_report = evaluate_route_quality(plan, routes)
        state.route_quality_plan_fingerprint = routes.plan_fingerprint
        state.route_optimization_status = "skipped"
        state.schedule_quality_report = schedule
        state.schedule_quality_plan_fingerprint = routes.plan_fingerprint
        state.schedule_optimization_status = "skipped"
        state.constraint_report = constraints
        state.constraint_plan_fingerprint = constraint_plan_fingerprint(request, plan)
        state.constraint_optimization_status = "skipped"
        return state

    @staticmethod
    def attach_candidate_reports(state: AgentState, *, constraint_error: bool = False) -> None:
        assert state.trip_plan is not None
        routes = empty_routes(state.request, state.trip_plan)
        state.route_estimates = routes.model_dump(mode="json")
        state.route_plan_fingerprint = routes.plan_fingerprint
        state.route_quality_report = evaluate_route_quality(state.trip_plan, routes)
        state.route_quality_plan_fingerprint = routes.plan_fingerprint
        state.schedule_quality_report = ScheduleTimelineEvaluator().evaluate(
            state.request,
            state.trip_plan,
            routes,
        )
        state.schedule_quality_plan_fingerprint = routes.plan_fingerprint
        report = ConstraintEvaluator().evaluate(
            state.request,
            state.trip_plan,
            state.schedule_quality_report,
            attractions=state.attractions,
            weather=state.weather,
        )
        if constraint_error:
            report = report.model_copy(update={"error_count": 1, "feasible": False})
        state.constraint_report = report
        state.constraint_plan_fingerprint = constraint_plan_fingerprint(
            state.request,
            state.trip_plan,
        )

    def test_refill_candidate_is_accepted_after_verified_reports(self):
        state = self.make_ready_state()
        orchestrator = TripOrchestrator(
            tool_registry=ToolRegistry(),
            minimum_total_attractions=1,
        )

        orchestrator.execute_action(state, AgentAction.REFILL_ATTRACTIONS)
        self.assertEqual(state.content_refill_status, "candidate_pending")
        self.attach_candidate_reports(state)
        orchestrator.execute_action(state, AgentAction.REFILL_ATTRACTIONS)

        self.assertEqual(state.content_refill_status, "completed")
        self.assertEqual(count_attractions(state.trip_plan), 1)
        self.assertEqual(state.content_refill_history[-1].status, "accepted")

    def test_refill_candidate_is_reverted_and_excluded_after_failed_verification(self):
        state = self.make_ready_state()
        baseline = state.trip_plan.model_dump(mode="json")
        orchestrator = TripOrchestrator(
            tool_registry=ToolRegistry(),
            minimum_total_attractions=1,
        )

        orchestrator.execute_action(state, AgentAction.REFILL_ATTRACTIONS)
        self.attach_candidate_reports(state, constraint_error=True)
        orchestrator.execute_action(state, AgentAction.REFILL_ATTRACTIONS)

        self.assertEqual(state.trip_plan.model_dump(mode="json"), baseline)
        self.assertEqual(state.content_refill_history[-1].status, "reverted")
        self.assertIn("id:near", state.content_refill_excluded_identities)


class MinimumAttractionValidationTests(unittest.TestCase):
    def test_minimum_attraction_violation_is_nonrepairable_error(self):
        request = make_request(days=3)
        plan = make_plan(
            days=3,
            attractions_by_day=[[attraction("Only One", 120.16)], [], []],
        )

        result = TripPlanValidator(minimum_total_attractions=2).validate(
            request,
            plan,
            attractions=candidates({"name": "Only One", "longitude": 120.16}),
            weather={"forecasts": []},
            hotels={"candidates": []},
        )
        issue = next(item for item in result.issues if item.code == "plan.insufficient_attractions")

        self.assertEqual(issue.severity.value, "error")
        self.assertFalse(issue.repairable)
        self.assertFalse(result.valid)
        self.assertFalse(result.repairable)


if __name__ == "__main__":
    unittest.main()
