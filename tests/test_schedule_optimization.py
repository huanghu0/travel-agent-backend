import unittest

from app.agent_runtime import AgentAction, AgentState, TripOrchestrator
from app.providers.amap.models import RouteEstimate, RouteEstimateResult
from app.routing import evaluate_route_quality, plan_route_fingerprint
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import DeterministicScheduleOptimizer, ScheduleTimelineEvaluator
from app.tools.models import ActionResult
from app.tools.registry import ToolRegistry
from app.validation import TripPlanValidator


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


def attraction(name: str, longitude: float, visit_duration: int = 180) -> dict:
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


def make_plan(
    first_day=("A", "B", "C"),
    second_day=(),
    *,
    visit_duration: int = 180,
) -> TripPlan:
    longitudes = {"A": 104.00, "B": 104.02, "C": 104.04, "D": 104.06}
    return TripPlan.model_validate(
        {
            "city": "Test City",
            "start_date": "2026-08-10",
            "end_date": "2026-08-11",
            "days": [
                {
                    "date": "2026-08-10",
                    "day_index": 0,
                    "description": "Day one",
                    "transportation": "driving",
                    "accommodation": "hotel",
                    "attractions": [
                        attraction(name, longitudes[name], visit_duration)
                        for name in first_day
                    ],
                    "meals": [{"type": "lunch", "name": "Local lunch"}],
                },
                {
                    "date": "2026-08-11",
                    "day_index": 1,
                    "description": "Day two",
                    "transportation": "driving",
                    "accommodation": "hotel",
                    "attractions": [
                        attraction(name, longitudes[name], visit_duration)
                        for name in second_day
                    ],
                    "meals": [],
                },
            ],
            "weather_info": [],
            "overall_suggestions": "Book ahead.",
            "budget": None,
        }
    )


def make_routes(
    plan: TripPlan,
    *,
    duration_seconds: int = 1800,
    available: bool = True,
) -> RouteEstimateResult:
    request = make_request()
    routes = []
    requested = 0
    for day_position, day in enumerate(plan.days):
        day_index = day.day_index if day.day_index >= 0 else day_position
        for leg_index in range(max(0, len(day.attractions) - 1)):
            requested += 1
            routes.append(
                RouteEstimate(
                    day_index=day_index,
                    leg_index=leg_index,
                    date=day.date,
                    origin_name=day.attractions[leg_index].name,
                    destination_name=day.attractions[leg_index + 1].name,
                    mode="driving",
                    available=available,
                    distance_meters=2000 if available else None,
                    duration_seconds=duration_seconds if available else None,
                    error_code=None if available else "NO_ROUTE",
                )
            )
    return RouteEstimateResult(
        plan_fingerprint=plan_route_fingerprint(request, plan),
        requested_legs=requested,
        evaluated_legs=len(routes),
        failed_legs=0 if available else len(routes),
        routes=routes,
    )


class ScheduleTimelineEvaluatorTests(unittest.TestCase):
    def test_real_route_duration_is_used_in_timeline(self):
        plan = make_plan(("A", "B"), (), visit_duration=180)
        evaluator = ScheduleTimelineEvaluator()

        report = evaluator.evaluate(make_request(), plan, make_routes(plan))

        day = report.days[0]
        transport = [item for item in day.timeline if item.item_type == "transportation"]
        self.assertEqual(day.transportation_minutes, 30)
        self.assertEqual(transport[0].transportation_time_source, "amap")
        self.assertEqual(day.fallback_route_legs, 0)

    def test_missing_route_uses_nonzero_haversine_fallback(self):
        plan = make_plan(("A", "B"), (), visit_duration=60)
        report = ScheduleTimelineEvaluator().evaluate(make_request(), plan, None)

        day = report.days[0]
        self.assertGreater(day.transportation_minutes, 0)
        self.assertEqual(day.fallback_route_legs, 1)
        transport = [item for item in day.timeline if item.item_type == "transportation"]
        self.assertEqual(transport[0].transportation_time_source, "haversine_fallback")

    def test_route_crossing_noon_inserts_lunch_before_next_attraction(self):
        plan = make_plan(("A", "B"), (), visit_duration=60)
        report = ScheduleTimelineEvaluator().evaluate(
            make_request(),
            plan,
            make_routes(plan, duration_seconds=7200),
        )

        timeline = report.days[0].timeline
        lunch_index = next(
            index for index, item in enumerate(timeline) if item.item_type == "meal"
        )
        second_attraction_index = next(
            index
            for index, item in enumerate(timeline)
            if item.item_type == "attraction" and item.name == "B"
        )
        self.assertLess(lunch_index, second_attraction_index)
        self.assertEqual(report.days[0].meal_minutes, 60)

    def test_lunch_can_start_at_configured_window_before_next_attraction(self):
        plan = make_plan(("A", "B"), (), visit_duration=150)
        report = ScheduleTimelineEvaluator(lunch_window_start="11:30").evaluate(
            make_request(),
            plan,
            make_routes(plan, duration_seconds=600),
        )

        meal = next(item for item in report.days[0].timeline if item.item_type == "meal")
        self.assertGreaterEqual(meal.start_time, "11:30")
        self.assertLess(meal.start_time, "13:30")
        second = next(
            item
            for item in report.days[0].timeline
            if item.item_type == "attraction" and item.name == "B"
        )
        self.assertLess(
            report.days[0].timeline.index(meal),
            report.days[0].timeline.index(second),
        )

    def test_duration_categories_and_overtime_are_aggregated(self):
        plan = make_plan(("A", "B", "C"), (), visit_duration=180)
        report = ScheduleTimelineEvaluator().evaluate(
            make_request(),
            plan,
            make_routes(plan, duration_seconds=1800),
        )

        day = report.days[0]
        self.assertEqual(day.attraction_minutes, 540)
        self.assertEqual(day.transportation_minutes, 60)
        self.assertEqual(day.meal_minutes, 60)
        self.assertEqual(day.break_minutes, 40)
        self.assertEqual(day.total_required_minutes, 700)
        self.assertEqual(day.overtime_minutes, 160)
        self.assertFalse(day.feasible)
        self.assertTrue(report.optimization_recommended)

    def test_unresolved_overtime_is_exposed_to_repair_validation(self):
        request = make_request()
        plan = make_plan(("A", "B", "C"), (), visit_duration=180)
        routes = make_routes(plan, duration_seconds=1800)
        report = ScheduleTimelineEvaluator().evaluate(request, plan, routes)

        result = TripPlanValidator().validate(
            request,
            plan,
            route_estimates=routes,
            schedule_quality_report=report,
        )

        self.assertFalse(result.valid)
        self.assertTrue(result.repairable)
        self.assertIn("schedule.daily_overtime", {item.code for item in result.issues})

    def test_same_input_produces_identical_timeline(self):
        plan = make_plan(("A", "B"), (), visit_duration=120)
        routes = make_routes(plan)
        evaluator = ScheduleTimelineEvaluator()

        first = evaluator.evaluate(make_request(), plan, routes)
        second = evaluator.evaluate(make_request(), plan, routes)

        self.assertEqual(first.model_dump(), second.model_dump())

    def test_empty_and_single_attraction_days_are_supported(self):
        plan = make_plan(("A",), (), visit_duration=60)
        report = ScheduleTimelineEvaluator().evaluate(make_request(), plan, None)

        self.assertEqual(report.days[0].transportation_minutes, 0)
        self.assertEqual(report.days[0].fallback_route_legs, 0)
        self.assertEqual(report.days[1].total_required_minutes, 0)
        self.assertTrue(report.days[1].feasible)


class DeterministicScheduleOptimizerTests(unittest.TestCase):
    def test_optimizer_moves_only_one_attraction_without_mutating_input(self):
        request = make_request()
        plan = make_plan()
        original = plan.model_dump()
        evaluator = ScheduleTimelineEvaluator()
        report = evaluator.evaluate(request, plan, make_routes(plan))
        optimizer = DeterministicScheduleOptimizer(evaluator=evaluator, max_candidates=6)

        candidate = optimizer.optimize(request, plan, report)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(plan.model_dump(), original)
        original_names = sorted(
            item.name for day in plan.days for item in day.attractions
        )
        candidate_names = sorted(
            item.name for day in candidate.plan.days for item in day.attractions
        )
        self.assertEqual(candidate_names, original_names)
        self.assertEqual(len(plan.days[0].attractions) - len(candidate.plan.days[0].attractions), 1)
        self.assertEqual(len(candidate.plan.days[1].attractions) - len(plan.days[1].attractions), 1)
        self.assertEqual(
            [day.date for day in candidate.plan.days],
            [day.date for day in plan.days],
        )
        source = next(item for item in plan.days[0].attractions if item.name == candidate.moved_attraction_name)
        moved = next(
            item
            for item in candidate.plan.days[1].attractions
            if item.name == candidate.moved_attraction_name
        )
        self.assertEqual(moved.visit_duration, source.visit_duration)
        self.assertLessEqual(candidate.considered_candidates, 6)

    def test_optimizer_is_deterministic(self):
        request = make_request()
        plan = make_plan()
        evaluator = ScheduleTimelineEvaluator()
        report = evaluator.evaluate(request, plan, make_routes(plan))
        optimizer = DeterministicScheduleOptimizer(evaluator=evaluator, max_candidates=6)

        first = optimizer.optimize(request, plan, report)
        second = optimizer.optimize(request, plan, report)

        self.assertEqual(first.model_dump(), second.model_dump())

    def test_optimizer_sheds_load_when_no_target_day_can_accept_it(self):
        request = make_request()
        plan = make_plan()
        plan.days = [plan.days[0]]
        original = plan.model_dump()
        evaluator = ScheduleTimelineEvaluator()
        report = evaluator.evaluate(request, plan, None)

        candidate = DeterministicScheduleOptimizer(
            evaluator=evaluator,
            max_candidates=6,
        ).optimize(request, plan, report)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(plan.model_dump(), original)
        self.assertEqual(candidate.strategy, "remove_attractions_from_overloaded_days")
        self.assertIsNone(candidate.target_day_index)
        self.assertTrue(candidate.removed_attraction_names)
        self.assertLess(
            evaluator.evaluate(request, candidate.plan, None).optimization_cost,
            report.optimization_cost,
        )

    def test_optimizer_removes_multiple_attractions_until_overload_is_resolved(self):
        request = make_request()
        plan = make_plan(("A", "B", "C"), (), visit_duration=360)
        plan.days = [plan.days[0]]
        evaluator = ScheduleTimelineEvaluator()
        report = evaluator.evaluate(request, plan, None)
        optimizer = DeterministicScheduleOptimizer(evaluator=evaluator, max_candidates=10)

        candidate = optimizer.optimize(request, plan, report)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.strategy, "remove_attractions_from_overloaded_days")
        self.assertGreaterEqual(len(candidate.removed_attraction_names), 2)
        optimized = evaluator.evaluate(request, candidate.plan, None)
        self.assertEqual(optimized.total_overtime_minutes, 0)
        self.assertLessEqual(candidate.considered_candidates, 10)


class ScheduleOrchestratorTests(unittest.TestCase):
    def make_state(self):
        request = make_request()
        plan = make_plan()
        routes = make_routes(plan, duration_seconds=600)
        evaluator = ScheduleTimelineEvaluator()
        state = AgentState.create(request, max_schedule_optimization_attempts=1)
        state.attractions = {"candidates": []}
        state.weather = {"forecasts": []}
        state.hotels = {"candidates": []}
        state.trip_plan = plan
        state.route_estimates = routes.model_dump(mode="json")
        state.route_plan_fingerprint = routes.plan_fingerprint
        state.route_quality_report = evaluate_route_quality(plan, routes)
        state.route_quality_plan_fingerprint = routes.plan_fingerprint
        state.route_optimization_status = "skipped"
        state.schedule_quality_report = evaluator.evaluate(request, plan, routes)
        state.schedule_quality_plan_fingerprint = routes.plan_fingerprint
        return state

    def test_overtime_enters_schedule_optimization_without_call_budget(self):
        state = self.make_state()
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        before_tools = state.tool_call_count
        before_llm = state.llm_call_count

        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.OPTIMIZE_SCHEDULE,
        )
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)

        self.assertEqual(state.schedule_optimization_status, "candidate_pending")
        self.assertEqual(state.tool_call_count, before_tools)
        self.assertEqual(state.llm_call_count, before_llm)
        self.assertEqual(state.route_optimization_status, "skipped")
        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.ESTIMATE_ROUTES,
        )

    def test_candidate_real_improvement_is_accepted(self):
        state = self.make_state()
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)
        assert state.trip_plan is not None
        candidate_routes = make_routes(state.trip_plan, duration_seconds=600)
        orchestrator._apply_tool_result(
            state,
            AgentAction.ESTIMATE_ROUTES,
            ActionResult(tool_name="estimate_routes", success=True, data=candidate_routes),
        )
        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.OPTIMIZE_SCHEDULE,
        )

        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)

        self.assertEqual(state.schedule_optimization_status, "completed")
        self.assertEqual(state.schedule_optimization_history[-1].status, "accepted")
        self.assertEqual(state.route_optimization_status, "skipped")
        self.assertEqual(state.schedule_optimization_count, 1)

    def test_candidate_below_configured_threshold_restores_baseline(self):
        state = self.make_state()
        original = state.trip_plan.model_dump()
        orchestrator = TripOrchestrator(
            tool_registry=ToolRegistry(),
            schedule_optimization_min_improvement_percent=101.0,
        )
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)
        assert state.trip_plan is not None
        candidate_routes = make_routes(state.trip_plan, duration_seconds=600)
        orchestrator._apply_tool_result(
            state,
            AgentAction.ESTIMATE_ROUTES,
            ActionResult(tool_name="estimate_routes", success=True, data=candidate_routes),
        )

        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)

        self.assertEqual(state.schedule_optimization_history[-1].status, "reverted")
        self.assertEqual(state.trip_plan.model_dump(), original)

    def test_candidate_route_regression_restores_baseline(self):
        state = self.make_state()
        original = state.trip_plan.model_dump()
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)
        assert state.trip_plan is not None
        candidate_routes = make_routes(state.trip_plan, duration_seconds=7200)
        orchestrator._apply_tool_result(
            state,
            AgentAction.ESTIMATE_ROUTES,
            ActionResult(tool_name="estimate_routes", success=True, data=candidate_routes),
        )

        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)

        self.assertEqual(state.schedule_optimization_history[-1].status, "reverted")
        self.assertEqual(state.trip_plan.model_dump(), original)
        self.assertEqual(state.schedule_optimization_status, "completed")

    def test_schedule_optimization_attempt_is_bounded(self):
        state = self.make_state()
        state.execution_budget.max_schedule_optimization_attempts = 0
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())

        orchestrator.execute_action(state, AgentAction.OPTIMIZE_SCHEDULE)

        self.assertEqual(state.schedule_optimization_status, "skipped")
        self.assertEqual(state.schedule_optimization_count, 0)
        self.assertEqual(state.schedule_optimization_history[-1].status, "skipped")


if __name__ == "__main__":
    unittest.main()

