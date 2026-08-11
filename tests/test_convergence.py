import tempfile
import unittest
from pathlib import Path

from app.agent_runtime import (
    ActionRecord,
    AgentAction,
    AgentConvergenceError,
    AgentState,
    TripOrchestrator,
)
from app.agent_runtime.convergence import (
    action_input_fingerprint,
    business_state_fingerprint,
    commute_input_fingerprint,
    constraint_input_fingerprint,
    route_quality_input_fingerprint,
    schedule_input_fingerprint,
    validation_input_fingerprint,
)
from app.memory import SQLiteAgentStateStore
from app.providers.amap.models import RouteEstimateResult
from app.routing import plan_route_fingerprint
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleQualityReport
from app.constraints import TripConstraintReport
from app.tools import ToolRegistry


def make_request() -> TripRequest:
    return TripRequest(
        city="杭州",
        start_date="2026-08-12",
        end_date="2026-08-12",
        travel_days=1,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["自然风光"],
    )


def make_plan() -> TripPlan:
    return TripPlan.model_validate(
        {
            "city": "杭州",
            "start_date": "2026-08-12",
            "end_date": "2026-08-12",
            "days": [
                {
                    "date": "2026-08-12",
                    "day_index": 0,
                    "description": "西湖休闲游",
                    "transportation": "公共交通",
                    "accommodation": "经济型酒店",
                    "attractions": [],
                    "meals": [],
                }
            ],
            "weather_info": [],
            "overall_suggestions": "提前预约。",
            "budget": None,
        }
    )


def make_routes(plan: TripPlan, *, duration: int = 600) -> dict:
    fingerprint = plan_route_fingerprint(make_request(), plan)
    return RouteEstimateResult(
        plan_fingerprint=fingerprint,
        requested_legs=1,
        evaluated_legs=1,
        routes=[
            {
                "day_index": 0,
                "leg_index": 0,
                "origin_name": "酒店",
                "destination_name": "西湖",
                "mode": "transit",
                "duration_seconds": duration,
                "distance_meters": 3000,
            }
        ],
    ).model_dump(mode="json")


class ScriptedOrchestrator(TripOrchestrator):
    def __init__(self, actions, executor, **kwargs):
        super().__init__(tool_registry=ToolRegistry(), **kwargs)
        self.actions = list(actions)
        self.executor = executor
        self.decision_index = 0

    def decide_next_action(self, state):
        action = self.actions[min(self.decision_index, len(self.actions) - 1)]
        self.decision_index += 1
        return action

    def execute_action(self, state, action, *, attempt_in_run=None):
        state.current_step += 1
        attempt = state.next_attempt(action)
        success = self.executor(state, action, attempt)
        state.action_history.append(
            ActionRecord(
                step=state.current_step,
                action=action,
                reason="convergence test",
                attempt=attempt,
                success=success,
            )
        )


class EvaluationFingerprintTests(unittest.TestCase):
    def test_route_based_fingerprints_change_with_route_snapshot(self):
        request = make_request()
        plan = make_plan()
        routes_a = make_routes(plan, duration=600)
        routes_b = make_routes(plan, duration=900)

        for factory in (
            route_quality_input_fingerprint,
            commute_input_fingerprint,
            schedule_input_fingerprint,
        ):
            self.assertEqual(factory(request, plan, routes_a), factory(request, plan, routes_a))
            self.assertNotEqual(factory(request, plan, routes_a), factory(request, plan, routes_b))

    def test_route_fingerprint_ignores_non_route_description_changes(self):
        request = make_request()
        plan = make_plan()
        routes = make_routes(plan)
        changed = plan.model_copy(deep=True)
        changed.overall_suggestions = "新的描述，不改变地点顺序。"

        self.assertEqual(
            route_quality_input_fingerprint(request, plan, routes),
            route_quality_input_fingerprint(request, changed, routes),
        )

    def test_constraint_and_validation_fingerprints_bind_derived_inputs(self):
        request = make_request()
        plan = make_plan()
        routes = make_routes(plan)
        plan_fp = plan_route_fingerprint(request, plan)
        schedule_a = ScheduleQualityReport(plan_fingerprint=plan_fp)
        schedule_b = ScheduleQualityReport(plan_fingerprint=plan_fp, total_overtime_minutes=30)
        weather_a = {"forecasts": [{"date": "2026-08-12", "day_weather": "晴"}]}
        weather_b = {"forecasts": [{"date": "2026-08-12", "day_weather": "暴雨"}]}

        self.assertNotEqual(
            constraint_input_fingerprint(request, plan, schedule_a, {}, weather_a),
            constraint_input_fingerprint(request, plan, schedule_a, {}, weather_b),
        )
        self.assertNotEqual(
            constraint_input_fingerprint(request, plan, schedule_a, {}, weather_a),
            constraint_input_fingerprint(request, plan, schedule_b, {}, weather_a),
        )

        constraint_a = TripConstraintReport(plan_fingerprint=plan_fp)
        constraint_b = TripConstraintReport(plan_fingerprint=plan_fp, warning_count=1)
        common = (request, plan, {}, weather_a, {}, routes, schedule_a)
        self.assertNotEqual(
            validation_input_fingerprint(*common, constraint_a),
            validation_input_fingerprint(*common, constraint_b),
        )


class LoopConvergenceTests(unittest.TestCase):
    def test_repeated_successful_action_input_stops_before_second_execution(self):
        calls = []

        def executor(state, action, attempt):
            calls.append(action)
            state.attractions = {"candidates": []}
            return True

        orchestrator = ScriptedOrchestrator(
            [AgentAction.SEARCH_ATTRACTIONS],
            executor,
            max_steps=8,
            max_repeated_action_inputs=1,
        )

        with self.assertRaises(AgentConvergenceError) as caught:
            orchestrator.run(make_request())

        self.assertEqual(calls, [AgentAction.SEARCH_ATTRACTIONS])
        self.assertEqual(caught.exception.state.status, "convergence_stopped")
        self.assertIn("相同业务输入", caught.exception.reason)

    def test_failed_attempt_is_not_counted_as_successful_duplicate(self):
        calls = []

        def executor(state, action, attempt):
            calls.append((action, attempt))
            if action is AgentAction.SEARCH_ATTRACTIONS and attempt == 1:
                return False
            if action is AgentAction.SEARCH_ATTRACTIONS:
                state.attractions = {"candidates": []}
                return True
            state.finished = True
            state.status = "completed"
            return True

        orchestrator = ScriptedOrchestrator(
            [
                AgentAction.SEARCH_ATTRACTIONS,
                AgentAction.SEARCH_ATTRACTIONS,
                AgentAction.FINISH,
            ],
            executor,
            max_steps=8,
        )
        state = orchestrator.run(make_request())

        self.assertTrue(state.finished)
        self.assertEqual([success for success in (r.success for r in state.action_history)], [False, True, True])

    def test_consecutive_no_progress_actions_stop_early(self):
        def executor(state, action, attempt):
            return True

        orchestrator = ScriptedOrchestrator(
            [
                AgentAction.GET_WEATHER,
                AgentAction.SEARCH_HOTELS,
                AgentAction.GENERATE_PLAN,
                AgentAction.FINISH,
            ],
            executor,
            max_steps=10,
            max_repeated_action_inputs=5,
            max_no_progress_steps=3,
        )

        with self.assertRaises(AgentConvergenceError) as caught:
            orchestrator.run(make_request())

        state = caught.exception.state
        self.assertEqual(state.current_step, 3)
        self.assertEqual(state.no_progress_streak, 3)
        self.assertEqual(state.no_progress_total, 3)
        self.assertEqual(len(state.convergence_history), 3)

    def test_progress_resets_no_progress_streak(self):
        state = AgentState.create(make_request(), max_no_progress_steps=3)
        orchestrator = TripOrchestrator(tool_registry=ToolRegistry())
        state.no_progress_streak = 2
        action = AgentAction.SEARCH_ATTRACTIONS
        input_fp = action_input_fingerprint(state, action)
        before = business_state_fingerprint(state)
        state.attractions = {"candidates": []}
        record = ActionRecord(
            step=1,
            action=action,
            reason="test",
            success=True,
        )
        state.action_history.append(record)

        orchestrator._record_convergence_result(
            state,
            action=action,
            input_fingerprint=input_fp,
            success_key=f"{action.value}:{input_fp}",
            state_fingerprint_before=before,
            action_record=record,
        )

        self.assertEqual(state.no_progress_streak, 0)
        self.assertTrue(record.made_progress)

    def test_sqlite_round_trip_preserves_convergence_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteAgentStateStore(Path(directory) / "memory.db")
            state = AgentState.create(make_request())
            state.evaluation_input_fingerprints["schedule"] = "schedule-fp"
            state.successful_action_inputs["finish:fp"] = 1
            state.no_progress_streak = 2
            state.no_progress_total = 4
            store.save_state(state)

            loaded = store.get_state(state.session_id)
            self.assertEqual(loaded.evaluation_input_fingerprints["schedule"], "schedule-fp")
            self.assertEqual(loaded.successful_action_inputs["finish:fp"], 1)
            self.assertEqual(loaded.no_progress_streak, 2)
            self.assertEqual(loaded.no_progress_total, 4)


if __name__ == "__main__":
    unittest.main()
