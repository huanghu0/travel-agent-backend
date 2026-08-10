import asyncio
import unittest

from app.commute import CommuteConstraintEvaluator
from app.agent_runtime import (
    AgentAction,
    AgentActionError,
    AgentState,
    AgentMaxStepsError,
    TripOrchestrator,
)
from app.constraints import ConstraintEvaluator, constraint_plan_fingerprint
from app.plan_content import plan_content_source_fingerprint
from app.routing import plan_route_fingerprint
from app.schemas.trip_schema import TripPlan, TripPlanResponse, TripRequest
from app.scheduling import ScheduleTimelineEvaluator


def make_request() -> TripRequest:
    return TripRequest(
        city="成都",
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["美食", "历史"],
    )


def make_plan() -> dict:
    return {
        "city": "成都",
        "start_date": "2026-08-10",
        "end_date": "2026-08-11",
        "days": [
            {
                "date": "2026-08-10",
                "day_index": 0,
                "description": "游览成都城区",
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "attractions": [],
                "meals": [],
            },
            {
                "date": "2026-08-11",
                "day_index": 1,
                "description": "继续游览并返程",
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "attractions": [],
                "meals": [],
            },
        ],
        "weather_info": [],
        "overall_suggestions": "提前预约热门景点。",
        "budget": None,
    }


def make_invalid_plan() -> dict:
    plan = make_plan()
    plan["days"] = []
    return plan


class RecordingAttractionAgent:
    def __init__(self, calls, responses=None):
        self.calls = calls
        self.responses = list(responses or [{"pois": []}])

    def search_attractions(self, city, preferences):
        self.calls.append((AgentAction.SEARCH_ATTRACTIONS, city, preferences))
        return self.responses.pop(0)


class RecordingWeatherAgent:
    def __init__(self, calls, response=None):
        self.calls = calls
        self.response = response or {"forecasts": []}

    def get_city_weather(self, city):
        self.calls.append((AgentAction.GET_WEATHER, city))
        return self.response


class RecordingHotelAgent:
    def __init__(self, calls, response=None):
        self.calls = calls
        self.response = response or {"pois": []}

    def search_hotels(self, city):
        self.calls.append((AgentAction.SEARCH_HOTELS, city))
        return self.response


class RecordingPlannerAgent:
    def __init__(self, calls, response=None, repair_responses=None):
        self.calls = calls
        self.response = response or make_plan()
        self.repair_responses = list(repair_responses or [make_plan()])
        self.received = None
        self.repair_received = None

    def generate_plan(self, request, attractions, weather, hotels):
        self.calls.append((AgentAction.GENERATE_PLAN, request.city))
        self.received = (attractions, weather, hotels)
        return self.response

    def repair_plan(
        self,
        request,
        current_plan,
        validation_result,
        attractions,
        weather,
        hotels,
    ):
        self.calls.append((AgentAction.REPAIR_PLAN, request.city))
        self.repair_received = (current_plan, validation_result)
        return self.repair_responses.pop(0)


def make_orchestrator(
    *,
    attraction_responses=None,
    planner_response=None,
    repair_responses=None,
    max_steps=16,
    max_attempts_per_action=2,
    max_repair_attempts=2,
):
    calls = []
    attraction = RecordingAttractionAgent(calls, attraction_responses)
    weather = RecordingWeatherAgent(calls)
    hotel = RecordingHotelAgent(calls)
    planner = RecordingPlannerAgent(
        calls,
        response=planner_response,
        repair_responses=repair_responses,
    )
    orchestrator = TripOrchestrator(
        attraction_agent=attraction,
        weather_agent=weather,
        hotel_agent=hotel,
        planner_agent=planner,
        max_steps=max_steps,
        max_attempts_per_action=max_attempts_per_action,
        max_repair_attempts=max_repair_attempts,
    )
    return orchestrator, calls, planner


class TripOrchestratorTests(unittest.TestCase):
    def test_happy_path_runs_actions_in_deterministic_order(self):
        orchestrator, calls, _ = make_orchestrator()

        state = orchestrator.run(make_request(), session_id="session-test")

        self.assertEqual(
            [record.action for record in state.action_history],
            [
                AgentAction.SEARCH_ATTRACTIONS,
                AgentAction.GET_WEATHER,
                AgentAction.SEARCH_HOTELS,
                AgentAction.GENERATE_PLAN,
                AgentAction.ESTIMATE_ROUTES,
                AgentAction.EVALUATE_COMMUTE,
                AgentAction.REBUILD_PLAN_CONTENT,
                AgentAction.EVALUATE_CONSTRAINTS,
                AgentAction.VALIDATE_PLAN,
                AgentAction.FINISH,
            ],
        )
        self.assertEqual(
            [call[0] for call in calls],
            [
                AgentAction.SEARCH_ATTRACTIONS,
                AgentAction.GET_WEATHER,
                AgentAction.SEARCH_HOTELS,
                AgentAction.GENERATE_PLAN,
            ],
        )
        self.assertEqual(state.status, "completed")
        self.assertTrue(state.finished)
        self.assertEqual(state.current_step, 10)
        self.assertEqual(state.session_id, "session-test")
        self.assertEqual(state.trip_plan.city, "成都")
        self.assertTrue(state.last_validation_result.valid)
        self.assertEqual(len(state.validation_history), 1)

    def test_route_fingerprint_controls_route_refresh_before_validation(self):
        state = AgentState.create(make_request())
        state.attractions = {"pois": []}
        state.weather = {"forecasts": []}
        state.hotels = {"pois": []}
        state.trip_plan = TripPlan.model_validate(make_plan())

        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.ESTIMATE_ROUTES,
        )

        fingerprint = plan_route_fingerprint(state.request, state.trip_plan)
        state.route_plan_fingerprint = fingerprint
        state.route_estimates = {
            "provider": "amap",
            "plan_fingerprint": fingerprint,
            "requested_legs": 0,
            "evaluated_legs": 0,
            "truncated_legs": 0,
            "routes": [],
        }
        # Checkpoints written before route scoring are upgraded by a local
        # OPTIMIZE_ROUTES step before semantic validation.
        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.OPTIMIZE_ROUTES,
        )
        TripOrchestrator._refresh_route_quality(state)
        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.EVALUATE_COMMUTE,
        )
        state.commute_report = CommuteConstraintEvaluator().evaluate(
            state.request,
            state.trip_plan,
            state.route_estimates,
        )
        state.commute_plan_fingerprint = fingerprint
        state.commute_optimization_status = "skipped"
        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.EVALUATE_SCHEDULE,
        )
        state.schedule_quality_report = ScheduleTimelineEvaluator().evaluate(
            state.request,
            state.trip_plan,
            state.route_estimates,
        )
        state.schedule_quality_plan_fingerprint = fingerprint
        state.schedule_optimization_status = "skipped"
        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.REBUILD_PLAN_CONTENT,
        )
        state.plan_consistency_fingerprint = plan_content_source_fingerprint(
            state.request,
            state.trip_plan,
            state.route_estimates,
            state.schedule_quality_report,
        )
        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.EVALUATE_CONSTRAINTS,
        )
        state.constraint_report = ConstraintEvaluator().evaluate(
            state.request,
            state.trip_plan,
            state.schedule_quality_report,
            attractions=state.attractions,
            weather=state.weather,
        )
        state.constraint_plan_fingerprint = constraint_plan_fingerprint(
            state.request,
            state.trip_plan,
        )
        state.constraint_optimization_status = "skipped"
        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.VALIDATE_PLAN,
        )

        state.trip_plan.days[0].transportation = "walking"
        self.assertEqual(
            TripOrchestrator.decide_next_action(state),
            AgentAction.ESTIMATE_ROUTES,
        )

    def test_planner_receives_all_collected_results(self):
        orchestrator, _, planner = make_orchestrator()

        state = orchestrator.run(make_request())

        self.assertEqual(
            planner.received,
            (state.attractions, state.weather, state.hotels),
        )

    def test_invalid_plan_is_repaired_then_validated_again(self):
        orchestrator, calls, planner = make_orchestrator(
            planner_response=make_invalid_plan(),
            repair_responses=[make_plan()],
        )

        state = orchestrator.run(make_request())

        self.assertEqual(
            [record.action for record in state.action_history],
            [
                AgentAction.SEARCH_ATTRACTIONS,
                AgentAction.GET_WEATHER,
                AgentAction.SEARCH_HOTELS,
                AgentAction.GENERATE_PLAN,
                AgentAction.ESTIMATE_ROUTES,
                AgentAction.EVALUATE_COMMUTE,
                AgentAction.REBUILD_PLAN_CONTENT,
                AgentAction.EVALUATE_CONSTRAINTS,
                AgentAction.VALIDATE_PLAN,
                AgentAction.REPAIR_PLAN,
                AgentAction.ESTIMATE_ROUTES,
                AgentAction.EVALUATE_COMMUTE,
                AgentAction.REBUILD_PLAN_CONTENT,
                AgentAction.EVALUATE_CONSTRAINTS,
                AgentAction.VALIDATE_PLAN,
                AgentAction.FINISH,
            ],
        )
        self.assertEqual([call[0] for call in calls].count(AgentAction.REPAIR_PLAN), 1)
        self.assertEqual(state.repair_count, 1)
        self.assertEqual(len(state.validation_history), 2)
        self.assertFalse(state.validation_history[0].valid)
        self.assertTrue(state.validation_history[1].valid)
        self.assertIsNotNone(planner.repair_received)
        self.assertEqual(state.status, "completed")

    def test_repair_limit_stops_invalid_plan(self):
        orchestrator, calls, _ = make_orchestrator(
            planner_response=make_invalid_plan(),
            repair_responses=[make_invalid_plan()],
            max_repair_attempts=1,
        )

        with self.assertRaises(AgentActionError) as caught:
            orchestrator.run(make_request())

        state = caught.exception.state
        self.assertEqual(caught.exception.action, AgentAction.VALIDATE_PLAN)
        self.assertEqual(state.status, "failed")
        self.assertEqual(state.repair_count, 1)
        self.assertEqual([call[0] for call in calls].count(AgentAction.REPAIR_PLAN), 1)
        self.assertIn("自动修复次数已达到上限", str(caught.exception))
        self.assertFalse(state.action_history[-1].retryable)

    def test_nonrepairable_request_error_does_not_call_repair_tool(self):
        request = make_request().model_copy(update={"travel_days": 3})
        orchestrator, calls, _ = make_orchestrator(planner_response=make_plan())

        with self.assertRaises(AgentActionError) as caught:
            orchestrator.run(request)

        self.assertEqual(caught.exception.action, AgentAction.VALIDATE_PLAN)
        self.assertEqual([call[0] for call in calls].count(AgentAction.REPAIR_PLAN), 0)
        self.assertFalse(caught.exception.state.last_validation_result.repairable)

    def test_failed_tool_result_is_retried(self):
        orchestrator, calls, _ = make_orchestrator(
            attraction_responses=[
                {"error": "temporary failure"},
                {"pois": [{"name": "武侯祠"}]},
            ]
        )

        state = orchestrator.run(make_request())

        self.assertEqual(calls[0][0], AgentAction.SEARCH_ATTRACTIONS)
        self.assertEqual(calls[1][0], AgentAction.SEARCH_ATTRACTIONS)
        self.assertFalse(state.action_history[0].success)
        self.assertTrue(state.action_history[1].success)
        self.assertEqual(state.action_history[0].attempt, 1)
        self.assertEqual(state.action_history[1].attempt, 2)
        self.assertEqual(state.attempts_by_action["search_attractions"], 2)
        self.assertEqual(state.status, "completed")

    def test_repeated_failure_stops_at_attempt_limit(self):
        orchestrator, _, _ = make_orchestrator(
            attraction_responses=[
                {"error": "failure one"},
                {"error": "failure two"},
            ],
            max_attempts_per_action=2,
        )

        with self.assertRaises(AgentActionError) as caught:
            orchestrator.run(make_request())

        error = caught.exception
        self.assertEqual(error.action, AgentAction.SEARCH_ATTRACTIONS)
        self.assertEqual(error.attempt, 2)
        self.assertEqual(error.state.status, "failed")
        self.assertEqual(error.state.current_step, 2)
        self.assertEqual(len(error.state.action_history), 2)
        self.assertTrue(all(not record.success for record in error.state.action_history))

    def test_max_steps_prevents_unbounded_execution(self):
        orchestrator, _, _ = make_orchestrator(max_steps=3)

        with self.assertRaises(AgentMaxStepsError) as caught:
            orchestrator.run(make_request())

        state = caught.exception.state
        self.assertEqual(state.status, "max_steps_reached")
        self.assertFalse(state.finished)
        self.assertEqual(state.current_step, 3)
        self.assertEqual(
            [record.action for record in state.action_history],
            [
                AgentAction.SEARCH_ATTRACTIONS,
                AgentAction.GET_WEATHER,
                AgentAction.SEARCH_HOTELS,
            ],
        )

    def test_amap_authorization_failure_is_not_retried(self):
        orchestrator, calls, _ = make_orchestrator(
            attraction_responses=[
                {"status": "0", "info": "INVALID_USER_KEY"},
                {"status": "1", "pois": []},
            ]
        )

        with self.assertRaises(AgentActionError) as caught:
            orchestrator.run(make_request())

        state = caught.exception.state
        self.assertEqual(len(calls), 1)
        self.assertFalse(state.action_history[0].success)
        self.assertIn("INVALID_USER_KEY", state.action_history[0].error)
        self.assertEqual(state.action_history[0].error_type, "authorization")
        self.assertFalse(state.action_history[0].retryable)
        self.assertEqual(state.status, "failed")

    def test_endpoint_keeps_trip_plan_response_shape(self):
        import main

        state = make_orchestrator()[0].run(make_request())

        class CompletedOrchestrator:
            def run(self, request):
                return state

        original = main.trip_orchestrator
        main.trip_orchestrator = CompletedOrchestrator()
        try:
            response = asyncio.run(main.generate_trip_plan(make_request()))
        finally:
            main.trip_orchestrator = original

        self.assertIsInstance(response, TripPlanResponse)
        self.assertTrue(response.success)
        self.assertEqual(response.message, "旅行计划生成成功")
        self.assertEqual(response.data.city, "成都")
        self.assertEqual(response.session_id, state.session_id)
        self.assertEqual(response.execution_steps, state.current_step)


if __name__ == "__main__":
    unittest.main()
