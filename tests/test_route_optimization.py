import unittest

from app.agent_runtime import AgentAction, TripOrchestrator
from app.providers.amap.models import (
    AttractionCandidate,
    AttractionSearchResult,
    HotelSearchResult,
    RouteEstimate,
    RouteEstimateResult,
    WeatherSearchResult,
)
from app.routing import (
    DeterministicRouteOptimizer,
    evaluate_route_quality,
    is_route_quality_improvement,
    plan_route_fingerprint,
    route_quality_improvement_percent,
)
from app.schemas.trip_schema import TripPlan, TripRequest


def make_request() -> TripRequest:
    return TripRequest(
        city="Test City",
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="driving",
        accommodation="hotel",
        preferences=["history"],
    )


def attraction(name: str, longitude: float, visit_duration: int = 60) -> dict:
    return {
        "name": name,
        "address": f"{name} address",
        "location": {"longitude": longitude, "latitude": 30.0},
        "visit_duration": visit_duration,
        "description": f"Visit {name}",
        "category": "attraction",
        "rating": 4.5,
        "photos": [],
        "poi_id": name,
        "ticket_price": 0,
    }


def make_plan(order=("A", "C", "B")) -> TripPlan:
    longitude = {"A": 104.0, "B": 104.1, "C": 104.2}
    return TripPlan.model_validate(
        {
            "city": "Test City",
            "start_date": "2026-08-10",
            "end_date": "2026-08-11",
            "days": [
                {
                    "date": "2026-08-10",
                    "day_index": 0,
                    "description": "Main route",
                    "transportation": "driving",
                    "accommodation": "hotel",
                    "attractions": [
                        attraction(name, longitude[name], 45 + index)
                        for index, name in enumerate(order)
                    ],
                    "meals": [],
                },
                {
                    "date": "2026-08-11",
                    "day_index": 1,
                    "description": "Free day",
                    "transportation": "walking",
                    "accommodation": "hotel",
                    "attractions": [],
                    "meals": [],
                },
            ],
            "weather_info": [],
            "overall_suggestions": "Book ahead.",
            "budget": None,
        }
    )


def route_result(
    plan: TripPlan,
    *,
    values: list[tuple[bool, int | None, int | None]],
) -> RouteEstimateResult:
    fingerprint = plan_route_fingerprint(make_request(), plan)
    routes = []
    day = plan.days[0]
    for leg_index, (available, distance, duration) in enumerate(values):
        routes.append(
            RouteEstimate(
                day_index=0,
                leg_index=leg_index,
                date=day.date,
                origin_name=day.attractions[leg_index].name,
                destination_name=day.attractions[leg_index + 1].name,
                mode="driving",
                available=available,
                distance_meters=distance,
                duration_seconds=duration,
                error_code=None if available else "NO_ROUTE",
            )
        )
    return RouteEstimateResult(
        plan_fingerprint=fingerprint,
        requested_legs=2,
        evaluated_legs=len(routes),
        truncated_legs=max(0, 2 - len(routes)),
        routes=routes,
    )


class RouteQualityTests(unittest.TestCase):
    def test_quality_aggregates_real_route_metrics(self):
        plan = make_plan()
        result = route_result(
            plan,
            values=[(True, 10_000, 1_200), (True, 20_000, 2_400)],
        )

        report = evaluate_route_quality(plan, result)

        self.assertEqual(report.total_legs, 2)
        self.assertEqual(report.available_legs, 2)
        self.assertEqual(report.unavailable_legs, 0)
        self.assertEqual(report.total_distance_meters, 30_000)
        self.assertEqual(report.total_duration_seconds, 3_600)
        self.assertEqual(report.optimization_cost, 6_600.0)
        self.assertEqual(report.days[0].longest_leg_index, 1)
        self.assertFalse(report.optimization_recommended)

    def test_missing_and_unavailable_legs_receive_penalties(self):
        plan = make_plan()
        result = route_result(plan, values=[(False, None, None)])

        report = evaluate_route_quality(plan, result)

        self.assertEqual(report.total_legs, 2)
        self.assertEqual(report.unavailable_legs, 2)
        self.assertEqual(report.optimization_cost, 43_200.0)
        self.assertTrue(report.optimization_recommended)

    def test_long_route_recommends_optimization(self):
        plan = make_plan()
        result = route_result(
            plan,
            values=[(True, 90_000, 7_200), (True, 1_000, 600)],
        )

        report = evaluate_route_quality(plan, result)

        self.assertEqual(report.excessive_duration_legs, 1)
        self.assertEqual(report.long_distance_legs, 1)
        self.assertTrue(report.optimization_recommended)

    def test_real_improvement_requires_threshold_and_no_hard_regression(self):
        plan = make_plan()
        before = evaluate_route_quality(
            plan,
            route_result(plan, values=[(True, 50_000, 4_000), (True, 50_000, 4_000)]),
        )
        after = evaluate_route_quality(
            plan,
            route_result(plan, values=[(True, 10_000, 1_000), (True, 10_000, 1_000)]),
        )

        self.assertGreater(route_quality_improvement_percent(before, after), 10)
        self.assertTrue(
            is_route_quality_improvement(
                before,
                after,
                min_improvement_percent=10,
            )
        )


class DeterministicRouteOptimizerTests(unittest.TestCase):
    def test_candidate_reorders_only_one_day_without_mutating_input(self):
        plan = make_plan()
        original_dump = plan.model_dump(mode="json")
        optimizer = DeterministicRouteOptimizer(max_candidates=6)

        candidate = optimizer.optimize(plan)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual([item.name for item in plan.days[0].attractions], ["A", "C", "B"])
        self.assertEqual(plan.model_dump(mode="json"), original_dump)
        self.assertEqual([item.name for item in candidate.plan.days[0].attractions], ["A", "B", "C"])
        self.assertEqual(candidate.plan.days[0].date, plan.days[0].date)
        self.assertEqual(candidate.plan.days[1], plan.days[1])
        self.assertCountEqual(
            [item.name for item in candidate.plan.days[0].attractions],
            [item.name for item in plan.days[0].attractions],
        )
        before_durations = sorted(item.visit_duration for item in plan.days[0].attractions)
        after_durations = sorted(item.visit_duration for item in candidate.plan.days[0].attractions)
        self.assertEqual(before_durations, after_durations)
        self.assertLessEqual(candidate.considered_candidates, 6)

    def test_optimizer_is_deterministic(self):
        optimizer = DeterministicRouteOptimizer(max_candidates=6)

        first = optimizer.optimize(make_plan())
        second = optimizer.optimize(make_plan())

        self.assertEqual(first, second)

    def test_already_short_order_has_no_better_candidate(self):
        optimizer = DeterministicRouteOptimizer(max_candidates=6)

        self.assertIsNone(optimizer.optimize(make_plan(("A", "B", "C"))))


class RecordingRouteProvider:
    def __init__(self, mode: str):
        self.mode = mode
        self.route_orders: list[tuple[tuple[str, str], ...]] = []

    def search_attractions(self, *, city, keywords):
        return AttractionSearchResult(
            query_city=city,
            keywords=keywords,
            total_received=3,
            candidates=[
                AttractionCandidate(
                    poi_id=name,
                    name=name,
                    address=f"{name} address",
                    location={"longitude": longitude, "latitude": 30.0},
                    category="attraction",
                )
                for name, longitude in (("A", 104.0), ("B", 104.1), ("C", 104.2))
            ],
        )

    def get_weather(self, city):
        return WeatherSearchResult(query_city=city, city=city, forecasts=[])

    def search_hotels(self, *, city, keywords):
        return HotelSearchResult(
            query_city=city,
            keywords=keywords,
            total_received=0,
            candidates=[],
        )

    def estimate_routes(self, *, city, plan_fingerprint, legs):
        order = tuple((leg.origin.name, leg.destination.name) for leg in legs)
        self.route_orders.append(order)
        routes = []
        for leg in legs:
            pair = (leg.origin.name, leg.destination.name)
            if self.mode == "accept":
                distance, duration = (
                    (90_000, 7_200)
                    if pair in {("A", "C"), ("C", "B")}
                    else (1_000, 600)
                )
            else:
                distance, duration = (
                    (31_000, 100)
                    if pair in {("A", "C"), ("C", "B")}
                    else (29_400, 100)
                )
            routes.append(
                RouteEstimate(
                    day_index=leg.day_index,
                    leg_index=leg.leg_index,
                    date=leg.date,
                    origin_name=leg.origin.name,
                    destination_name=leg.destination.name,
                    mode=leg.mode,
                    distance_meters=distance,
                    duration_seconds=duration,
                )
            )
        return RouteEstimateResult(
            plan_fingerprint=plan_fingerprint,
            requested_legs=len(legs),
            evaluated_legs=len(legs),
            truncated_legs=0,
            routes=routes,
        )


class StaticPlanner:
    def generate_plan(self, request, attractions, weather, hotels):
        return make_plan()

    def repair_plan(self, *args, **kwargs):
        return make_plan()


class RouteOptimizationOrchestratorTests(unittest.TestCase):
    def make_orchestrator(self, mode="accept", **kwargs):
        provider = RecordingRouteProvider(mode)
        from app.tools import build_trip_tool_registry

        registry = build_trip_tool_registry(
            planner_agent=StaticPlanner(),
            map_provider=provider,
        )
        orchestrator = TripOrchestrator(
            tool_registry=registry,
            max_steps=16,
            max_route_optimization_attempts=kwargs.get(
                "max_route_optimization_attempts", 1
            ),
            route_optimization_max_candidates=6,
            route_optimization_min_improvement_percent=10,
        )
        return orchestrator, provider

    def test_candidate_is_accepted_after_real_route_improvement(self):
        orchestrator, provider = self.make_orchestrator("accept")

        state = orchestrator.run(make_request())

        self.assertEqual([item.name for item in state.trip_plan.days[0].attractions], ["A", "B", "C"])
        self.assertEqual(state.route_optimization_count, 1)
        self.assertEqual(state.route_optimization_status, "completed")
        self.assertEqual(state.route_optimization_history[-1].status, "accepted")
        self.assertGreater(state.route_optimization_history[-1].actual_improvement_percent, 10)
        self.assertEqual(len(provider.route_orders), 2)
        self.assertEqual(
            [record.action for record in state.action_history].count(
                AgentAction.OPTIMIZE_ROUTES
            ),
            2,
        )
        self.assertEqual(state.llm_call_count, 1)
        self.assertEqual(state.tool_call_count, 6)

    def test_candidate_below_real_improvement_threshold_is_reverted(self):
        orchestrator, provider = self.make_orchestrator("revert")

        state = orchestrator.run(make_request())

        self.assertEqual([item.name for item in state.trip_plan.days[0].attractions], ["A", "C", "B"])
        self.assertEqual(state.route_optimization_history[-1].status, "reverted")
        self.assertLess(state.route_optimization_history[-1].actual_improvement_percent, 10)
        self.assertEqual(len(provider.route_orders), 2)
        self.assertEqual(
            state.route_plan_fingerprint,
            plan_route_fingerprint(state.request, state.trip_plan),
        )

    def test_zero_optimization_budget_skips_candidate_route_call(self):
        orchestrator, provider = self.make_orchestrator(
            "accept",
            max_route_optimization_attempts=0,
        )

        state = orchestrator.run(make_request())

        self.assertEqual(len(provider.route_orders), 1)
        self.assertEqual(state.route_optimization_count, 0)
        self.assertNotIn(
            AgentAction.OPTIMIZE_ROUTES,
            [record.action for record in state.action_history],
        )


if __name__ == "__main__":
    unittest.main()
