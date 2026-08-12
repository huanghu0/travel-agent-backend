import tempfile
import unittest
from pathlib import Path

from app.commute import CommuteConstraintEvaluator
from app.agent_runtime import AgentAction, AgentState, TripOrchestrator
from app.constraints import ConstraintEvaluator, DeterministicConstraintOptimizer
from app.core.config import settings
from app.memory import SQLiteAgentStateStore
from app.plan_content import (
    plan_content_source_fingerprint,
    restaurant_search_source_fingerprint,
)
from app.providers.amap.models import RouteEstimate, RouteEstimateResult
from app.routing import evaluate_route_quality, plan_route_fingerprint
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleTimelineEvaluator
from app.tools.models import ActionResult
from app.tools.registry import ToolRegistry
from app.validation import TripPlanValidator


def make_request(*, free_text_input: str = "") -> TripRequest:
    return TripRequest(
        city="Test City",
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="driving",
        accommodation="hotel",
        preferences=["history"],
        free_text_input=free_text_input,
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


def make_plan(first_day=("A", "B"), second_day=(), *, visit_duration: int = 60) -> TripPlan:
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
                    "meals": [{"type": "lunch", "name": "Lunch"}],
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
                    "meals": [{"type": "lunch", "name": "Lunch"}],
                },
            ],
            "weather_info": [],
            "overall_suggestions": "Book ahead.",
            "budget": None,
        }
    )


def make_routes(
    request: TripRequest,
    plan: TripPlan,
    *,
    duration_seconds: int = 600,
    available: bool = True,
) -> RouteEstimateResult:
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


def candidates(*items) -> dict:
    return {"candidates": list(items)}


def source(name: str, **extra) -> dict:
    value = {
        "poi_id": name,
        "name": name,
        "address": f"{name} address",
        "location": {"longitude": 104.0, "latitude": 30.0},
        "category": "attraction",
    }
    value.update(extra)
    return value


class ConstraintEvaluatorTests(unittest.TestCase):
    def test_opening_hours_and_unknown_hours_are_handled_conservatively(self):
        request = make_request()
        plan = make_plan(("A", "B"), ())
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, None)
        evaluator = ConstraintEvaluator()

        report = evaluator.evaluate(
            request,
            plan,
            schedule,
            attractions=candidates(
                source("A", opening_hours="14:00-18:00"),
                source("B", opening_hours=""),
            ),
        )

        codes = [item.code for item in report.issues]
        self.assertIn("attraction.outside_opening_hours", codes)
        self.assertEqual(
            sum(item.attraction_name == "B" for item in report.issues),
            0,
        )

    def test_closed_date_and_user_period_preference_are_errors(self):
        request = make_request(free_text_input="\u4e0b\u5348A")
        plan = make_plan(("A", "B"), ())
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, None)

        report = ConstraintEvaluator().evaluate(
            request,
            plan,
            schedule,
            attractions=candidates(source("A", closed_dates=["2026-08-10"])),
        )

        codes = {item.code for item in report.issues}
        self.assertIn("attraction.closed_on_date", codes)
        self.assertIn("preference.wrong_time_period", codes)
        self.assertFalse(report.feasible)

    def test_weather_daily_load_and_same_input_are_deterministic(self):
        request = make_request()
        plan = make_plan(("A", "B", "C"), ())
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, None)
        evaluator = ConstraintEvaluator(daily_attraction_soft_limit=2)
        attraction_data = candidates(
            source("A", category="\u516c\u56ed"),
            source("B"),
            source("C"),
        )
        weather = {
            "forecasts": [
                {"date": "2026-08-10", "day_weather": "\u66b4\u96e8"}
            ]
        }

        first = evaluator.evaluate(
            request,
            plan,
            schedule,
            attractions=attraction_data,
            weather=weather,
        )
        second = evaluator.evaluate(
            request,
            plan,
            schedule,
            attractions=attraction_data,
            weather=weather,
        )

        codes = {item.code for item in first.issues}
        self.assertIn("weather.outdoor_risk", codes)
        self.assertIn("schedule.too_many_attractions", codes)
        self.assertEqual(first.model_dump(), second.model_dump())

    def test_late_lunch_is_reported(self):
        request = make_request()
        plan = make_plan(("A",), (), visit_duration=360)
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, None)

        report = ConstraintEvaluator().evaluate(request, plan, schedule)

        self.assertIn("meal.outside_time_window", {item.code for item in report.issues})


class ConstraintOptimizerTests(unittest.TestCase):
    def test_cross_day_candidate_fixes_closed_date_without_mutating_input(self):
        request = make_request()
        plan = make_plan(("A", "B"), ())
        original = plan.model_dump()
        attraction_data = candidates(source("A", closed_dates=["2026-08-10"]))
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, None)
        evaluator = ConstraintEvaluator()
        report = evaluator.evaluate(
            request,
            plan,
            schedule,
            attractions=attraction_data,
        )
        optimizer = DeterministicConstraintOptimizer(
            evaluator=evaluator,
            max_candidates=8,
        )

        first = optimizer.optimize(
            request,
            plan,
            report,
            attractions=attraction_data,
        )
        second = optimizer.optimize(
            request,
            plan,
            report,
            attractions=attraction_data,
        )

        self.assertIsNotNone(first)
        assert first is not None and second is not None
        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(first.strategy, "move_attraction_between_days")
        self.assertEqual(plan.model_dump(), original)
        self.assertLessEqual(first.considered_candidates, 8)
        before_names = sorted(item.name for day in plan.days for item in day.attractions)
        after_names = sorted(
            item.name for day in first.plan.days for item in day.attractions
        )
        self.assertEqual(after_names, before_names)

    def test_optimizer_removes_attraction_when_lunch_conflict_cannot_be_moved(self):
        request = make_request()
        plan = make_plan(("A",), (), visit_duration=360)
        plan.days = [plan.days[0]]
        schedule_evaluator = ScheduleTimelineEvaluator()
        schedule = schedule_evaluator.evaluate(request, plan, None)
        evaluator = ConstraintEvaluator()
        report = evaluator.evaluate(request, plan, schedule)
        optimizer = DeterministicConstraintOptimizer(
            evaluator=evaluator,
            schedule_evaluator=schedule_evaluator,
            max_candidates=8,
        )

        candidate = optimizer.optimize(request, plan, report)

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(
            candidate.strategy,
            "remove_attraction_for_constraint_feasibility",
        )
        self.assertEqual(candidate.removed_attraction_names, ["A"])
        self.assertEqual(candidate.plan.days[0].attractions, [])

    def test_optimizer_returns_none_without_repairable_issue(self):
        request = make_request()
        plan = make_plan(("A",), ())
        schedule = ScheduleTimelineEvaluator().evaluate(request, plan, None)
        evaluator = ConstraintEvaluator()
        report = evaluator.evaluate(request, plan, schedule)

        self.assertIsNone(
            DeterministicConstraintOptimizer(evaluator=evaluator).optimize(
                request,
                plan,
                report,
            )
        )


class ConstraintOrchestratorTests(unittest.TestCase):
    def make_state(self, *, attraction_names=("A", "B")):
        request = make_request()
        plan = make_plan(attraction_names, ())
        routes = make_routes(request, plan)
        evaluator = ScheduleTimelineEvaluator()
        state = AgentState.create(request, max_constraint_optimization_attempts=1)
        state.attractions = candidates(source("A", closed_dates=["2026-08-10"]))
        state.weather = {"forecasts": []}
        state.hotels = {"candidates": []}
        state.trip_plan = plan
        state.route_estimates = routes.model_dump(mode="json")
        state.route_plan_fingerprint = routes.plan_fingerprint
        state.route_quality_report = evaluate_route_quality(plan, routes)
        state.route_quality_plan_fingerprint = routes.plan_fingerprint
        state.route_optimization_status = "skipped"
        state.commute_report = CommuteConstraintEvaluator().evaluate(request, plan, routes)
        state.commute_plan_fingerprint = routes.plan_fingerprint
        state.commute_optimization_status = "skipped"
        state.schedule_quality_report = evaluator.evaluate(request, plan, routes)
        state.schedule_quality_plan_fingerprint = routes.plan_fingerprint
        state.schedule_optimization_status = "skipped"
        state.restaurants = {
            "provider": "amap",
            "query_city": request.city,
            "keywords": "餐厅",
            "candidates": [],
        }
        state.restaurant_plan_fingerprint = restaurant_search_source_fingerprint(
            plan,
            max_anchors=settings.AMAP_MAX_RESTAURANT_SEARCH_ANCHORS,
        )
        state.plan_consistency_fingerprint = plan_content_source_fingerprint(
            request,
            plan,
            routes,
            state.schedule_quality_report,
            state.restaurants,
        )
        return state

    def test_local_evaluation_and_candidate_do_not_consume_call_budgets(self):
        state = self.make_state()
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        before = (state.tool_call_count, state.llm_call_count)

        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.EVALUATE_CONSTRAINTS,
        )
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)
        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.OPTIMIZE_CONSTRAINTS,
        )
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_CONSTRAINTS)

        self.assertEqual((state.tool_call_count, state.llm_call_count), before)
        self.assertEqual(state.constraint_optimization_status, "candidate_pending")
        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.ESTIMATE_ROUTES,
        )

    def test_constraint_optimization_attempt_budget_is_bounded(self):
        state = self.make_state()
        state.execution_budget.max_constraint_optimization_attempts = 0
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_CONSTRAINTS)

        self.assertEqual(state.constraint_optimization_status, "skipped")
        self.assertEqual(state.constraint_optimization_count, 0)
        self.assertEqual(
            state.constraint_optimization_history[-1].reason,
            "Constraint optimization attempt budget is exhausted",
        )

    def test_candidate_is_accepted_only_after_real_route_and_constraint_recheck(self):
        state = self.make_state()
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_CONSTRAINTS)
        assert state.trip_plan is not None
        candidate_routes = make_routes(state.request, state.trip_plan)
        orchestrator._apply_tool_result(
            state,
            AgentAction.ESTIMATE_ROUTES,
            ActionResult(
                tool_name="estimate_routes",
                success=True,
                data=candidate_routes,
            ),
        )

        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.EVALUATE_CONSTRAINTS,
        )
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)
        self.assertEqual(
            orchestrator.decide_next_action(state),
            AgentAction.OPTIMIZE_CONSTRAINTS,
        )
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_CONSTRAINTS)

        self.assertEqual(state.constraint_optimization_status, "completed")
        self.assertEqual(state.constraint_optimization_history[-1].status, "accepted")
        self.assertEqual(state.constraint_report.error_count, 0)

    def test_route_regression_reverts_candidate(self):
        state = self.make_state(attraction_names=("A", "B", "C"))
        original = state.trip_plan.model_dump()
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_CONSTRAINTS)
        assert state.trip_plan is not None
        candidate_routes = make_routes(
            state.request,
            state.trip_plan,
            available=False,
        )
        orchestrator._apply_tool_result(
            state,
            AgentAction.ESTIMATE_ROUTES,
            ActionResult(
                tool_name="estimate_routes",
                success=True,
                data=candidate_routes,
            ),
        )
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_CONSTRAINTS)

        self.assertEqual(state.constraint_optimization_history[-1].status, "reverted")
        self.assertEqual(state.trip_plan.model_dump(), original)

    def test_sqlite_resume_preserves_pending_candidate(self):
        state = self.make_state()
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        orchestrator.execute_action(state, AgentAction.EVALUATE_CONSTRAINTS)
        orchestrator.execute_action(state, AgentAction.OPTIMIZE_CONSTRAINTS)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteAgentStateStore(Path(temp_dir) / "agent.db")
            store.save_state(state)
            loaded = store.get_state(state.session_id)

        self.assertEqual(loaded.constraint_optimization_status, "candidate_pending")
        self.assertIsNotNone(loaded.constraint_optimization_baseline_report)
        self.assertEqual(
            orchestrator.decide_next_action(loaded),
            AgentAction.ESTIMATE_ROUTES,
        )

    def test_unresolved_constraint_is_exposed_to_validation_repair_loop(self):
        state = self.make_state()
        evaluator = ConstraintEvaluator()
        state.constraint_report = evaluator.evaluate(
            state.request,
            state.trip_plan,
            state.schedule_quality_report,
            attractions=state.attractions,
        )

        result = TripPlanValidator().validate(
            state.request,
            state.trip_plan,
            attractions=state.attractions,
            route_estimates=state.route_estimates,
            schedule_quality_report=state.schedule_quality_report,
            constraint_report=state.constraint_report,
        )

        self.assertFalse(result.valid)
        self.assertIn("attraction.closed_on_date", {item.code for item in result.issues})
        self.assertTrue(result.repairable)


if __name__ == "__main__":
    unittest.main()
