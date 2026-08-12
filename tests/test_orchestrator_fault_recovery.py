import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.agent_runtime import (
    AgentActionError,
    AgentCheckpointError,
    CheckpointPolicy,
    CircuitState,
    TripOrchestrator,
)
from app.evaluation.fault_injection import (
    FaultInjectingProxy,
    FaultInjector,
    FaultMode,
    FaultRule,
)
from app.evaluation.orchestrator_faults import (
    FIXED_ORCHESTRATOR_FAULT_CASES,
    DeterministicAmapProvider,
    DeterministicPlannerAgent,
    get_orchestrator_fault_case,
    make_orchestrator_fault_request,
    run_orchestrator_fault_case,
)
from app.memory import SessionNotFoundError, SQLiteAgentStateStore
from app.tools import ToolErrorType, build_trip_tool_registry


class OrchestratorFaultRecoveryTests(unittest.TestCase):
    """Exercise failure recovery through the real deterministic runtime stack."""

    def test_fixed_suite_contains_six_full_orchestrator_cases(self):
        self.assertEqual(len(FIXED_ORCHESTRATOR_FAULT_CASES), 6)
        self.assertEqual(
            {case.case_id for case in FIXED_ORCHESTRATOR_FAULT_CASES},
            {
                "attraction-timeout-once",
                "hotel-rate-limit-once",
                "planner-invalid-output-once",
                "authorization-failure",
                "sqlite-locked-once",
                "route-partial-failure",
            },
        )

    def assert_recovered(self, case_id: str):
        result = run_orchestrator_fault_case(get_orchestrator_fault_case(case_id))
        self.assertIsNone(result.exception)
        self.assertTrue(result.completed)
        self.assertTrue(result.persisted)
        self.assertIsNotNone(result.persisted_state)
        self.assertEqual(result.persisted_state.status, "completed")
        self.assertEqual(result.resume_state.status, "completed")
        self.assertLessEqual(result.state.current_step, result.state.max_steps)
        self.assertLessEqual(
            result.state.tool_call_count,
            result.state.execution_budget.max_tool_calls,
        )
        self.assertLessEqual(
            result.state.llm_call_count,
            result.state.execution_budget.max_llm_calls,
        )
        self.assertNotEqual(result.state.status, "max_steps_reached")
        return result

    def test_attraction_timeout_recovers_on_second_call(self):
        result = self.assert_recovered("attraction-timeout-once")
        self.assertTrue(
            result.injector.was_triggered("search_attractions", FaultMode.TIMEOUT)
        )
        self.assertEqual(result.injector.call_counts["search_attractions"], 2)
        failed = [item for item in result.state.action_history if not item.success]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error_type, ToolErrorType.TIMEOUT)
        self.assertTrue(failed[0].retryable)

    def test_hotel_429_preserves_rate_limit_semantics_and_recovers(self):
        result = self.assert_recovered("hotel-rate-limit-once")
        self.assertTrue(
            result.injector.was_triggered("search_hotels", FaultMode.RATE_LIMIT)
        )
        self.assertEqual(result.injector.call_counts["search_hotels"], 2)
        failed = next(item for item in result.state.action_history if not item.success)
        self.assertEqual(failed.error_type, ToolErrorType.RATE_LIMIT)
        self.assertEqual(failed.provider_code, "429")
        self.assertEqual(failed.circuit_state, CircuitState.CLOSED.value)

    def test_invalid_planner_output_retries_generation_without_repair(self):
        result = self.assert_recovered("planner-invalid-output-once")
        self.assertTrue(
            result.injector.was_triggered("generate_plan", FaultMode.INVALID_OUTPUT)
        )
        self.assertEqual(result.injector.call_counts["generate_plan"], 2)
        self.assertEqual(result.state.llm_call_count, 2)
        self.assertEqual(result.planner_generate_calls, 1)
        self.assertNotIn(
            "repair_plan",
            [item.action.value for item in result.state.action_history],
        )
        failed = next(item for item in result.state.action_history if not item.success)
        self.assertEqual(failed.error_type, ToolErrorType.INVALID_OUTPUT)

    def test_authorization_failure_is_terminal_and_not_retried(self):
        result = run_orchestrator_fault_case(
            get_orchestrator_fault_case("authorization-failure")
        )
        self.assertIsInstance(result.exception, AgentActionError)
        self.assertEqual(result.state.status, "failed")
        self.assertFalse(result.state.finished)
        self.assertTrue(result.persisted)
        self.assertEqual(result.injector.call_counts["search_attractions"], 1)
        self.assertNotIn("get_weather", result.injector.call_counts)
        self.assertNotIn("search_hotels", result.injector.call_counts)
        self.assertNotIn("generate_plan", result.injector.call_counts)
        failed = result.state.action_history[-1]
        self.assertEqual(failed.error_type, ToolErrorType.AUTHORIZATION)
        self.assertFalse(failed.retryable)

    def test_sqlite_lock_retry_does_not_repeat_completed_tool(self):
        result = self.assert_recovered("sqlite-locked-once")
        self.assertTrue(
            result.injector.was_triggered("sqlite.save_state", FaultMode.SQLITE_LOCKED)
        )
        self.assertEqual(result.injector.call_counts["search_attractions"], 1)
        self.assertEqual(result.injector.call_counts["get_weather"], 1)
        self.assertEqual(result.injector.call_counts["search_hotels"], 1)
        self.assertEqual(result.injector.call_counts["generate_plan"], 1)
        self.assertEqual(result.injector.call_counts["estimate_routes"], 1)
        self.assertEqual(result.injector.call_counts["search_restaurants"], 1)

    def test_partial_route_failure_is_recovered_by_deterministic_reordering(self):
        result = self.assert_recovered("route-partial-failure")
        self.assertTrue(
            result.injector.was_triggered("estimate_route_segment", FaultMode.TIMEOUT)
        )
        self.assertEqual(result.provider_calls["estimate_routes"], 2)
        self.assertIsNotNone(result.state.route_estimates)
        self.assertEqual(result.state.route_estimates["failed_legs"], 0)
        self.assertEqual(result.state.route_quality_report.unavailable_legs, 0)
        self.assertEqual(result.state.route_optimization_status, "completed")
        self.assertEqual(result.state.route_optimization_history[-1].status, "accepted")
        self.assertEqual(result.state.commute_report.excessive_segment_count, 0)


class CheckpointPolicyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteAgentStateStore(
            Path(self.temp_dir.name) / "checkpoint-policy.db"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def build_orchestrator(store, policy):
        registry = build_trip_tool_registry(
            planner_agent=DeterministicPlannerAgent(),
            map_provider=DeterministicAmapProvider(),
        )
        return TripOrchestrator(
            tool_registry=registry,
            state_store=store,
            checkpoint_policy=policy,
            max_steps=16,
            retry_base_delay_seconds=0,
            retry_max_delay_seconds=0,
            retry_jitter_seconds=0,
            max_route_optimization_attempts=0,
            max_schedule_optimization_attempts=0,
            max_constraint_optimization_attempts=0,
        )

    def test_checkpoint_retry_exhaustion_raises_typed_runtime_error(self):
        injector = FaultInjector(
            [
                FaultRule(
                    target="sqlite.save_state",
                    mode=FaultMode.SQLITE_LOCKED,
                    call_numbers=[1, 2, 3],
                )
            ]
        )
        proxy = FaultInjectingProxy(self.store, injector, prefix="sqlite")
        policy = CheckpointPolicy(
            max_attempts=3,
            base_delay_seconds=0,
            max_delay_seconds=0,
            sleep_fn=lambda _: None,
        )
        orchestrator = self.build_orchestrator(proxy, policy)

        with self.assertRaises(AgentCheckpointError) as raised:
            orchestrator.run(
                make_orchestrator_fault_request(),
                session_id="checkpoint-exhausted",
            )

        self.assertEqual(raised.exception.state.status, "failed")
        self.assertEqual(injector.call_counts["sqlite.save_state"], 3)
        self.assertEqual(len(policy.retry_events), 2)
        with self.assertRaises(SessionNotFoundError):
            self.store.get_state("checkpoint-exhausted")

    def test_non_lock_operational_error_is_not_retried(self):
        class BrokenStore:
            calls = 0

            def save_state(self, state):
                self.calls += 1
                raise sqlite3.OperationalError("no such table: agent_sessions")

        store = BrokenStore()
        policy = CheckpointPolicy(
            max_attempts=3,
            base_delay_seconds=0,
            max_delay_seconds=0,
            sleep_fn=lambda _: None,
        )
        orchestrator = self.build_orchestrator(store, policy)

        with self.assertRaises(AgentCheckpointError):
            orchestrator.run(make_orchestrator_fault_request())

        self.assertEqual(store.calls, 1)
        self.assertEqual(policy.retry_events, [])


if __name__ == "__main__":
    unittest.main()
