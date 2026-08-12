import tempfile
import unittest
from pathlib import Path

from pydantic import BaseModel

from app.agent_runtime import AgentState, ExecutionPolicy
from app.evaluation import (
    FIXED_FAULT_SCENARIOS,
    FaultInjectingProxy,
    FaultInjector,
    FaultMode,
    FaultRule,
)
from app.tools import ToolDefinition, ToolRegistry
from app.memory import SQLiteAgentStateStore
from app.schemas.trip_schema import TripRequest
from app.tools.models import ToolErrorType


class EchoInput(BaseModel):
    value: int


class EchoOutput(BaseModel):
    value: int



class FaultInjectionTests(unittest.TestCase):
    def test_fixed_fault_suite_covers_provider_llm_and_sqlite(self):
        targets = {
            rule.target
            for scenario in FIXED_FAULT_SCENARIOS
            for rule in scenario.rules
        }
        self.assertIn("search_attractions", targets)
        self.assertIn("generate_plan", targets)
        self.assertIn("sqlite.save_state", targets)

    def test_tool_registry_timeout_once_then_recovers(self):
        injector = FaultInjector(
            [
                FaultRule(
                    target="search_attractions",
                    mode=FaultMode.TIMEOUT,
                    call_numbers=[1],
                )
            ]
        )
        registry = ToolRegistry(call_injector=injector)
        registry.register(
            ToolDefinition(
                name="search_attractions",
                description="test",
                input_model=EchoInput,
                output_model=EchoOutput,
                handler=lambda item: {"value": item.value},
            )
        )

        first = registry.execute("search_attractions", {"value": 1})
        second = registry.execute("search_attractions", {"value": 2})

        self.assertFalse(first.success)
        self.assertEqual(first.error_type, ToolErrorType.TIMEOUT)
        self.assertTrue(first.retryable)
        self.assertTrue(second.success)
        self.assertEqual(second.data, {"value": 2})
        self.assertTrue(injector.was_triggered("search_attractions", FaultMode.TIMEOUT))

    def test_execution_policy_can_retry_injected_transient_failure(self):
        injector = FaultInjector(
            [FaultRule(target="search_attractions", mode=FaultMode.TIMEOUT)]
        )
        registry = ToolRegistry(call_injector=injector)
        registry.register(
            ToolDefinition(
                name="search_attractions",
                description="test",
                input_model=EchoInput,
                output_model=EchoOutput,
                handler=lambda item: {"value": item.value},
            )
        )
        policy = ExecutionPolicy(
            registry,
            retry_base_delay_seconds=0,
            retry_max_delay_seconds=0,
            retry_jitter_seconds=0,
        )
        state = AgentState.create(
            TripRequest(
                city="杭州",
                start_date="2026-10-12",
                end_date="2026-10-12",
                travel_days=1,
                transportation="公共交通",
                accommodation="经济型酒店",
                preferences=["休闲"],
            )
        )

        first = policy.execute_once(state, "search_attractions", {"value": 1})
        decision = policy.decide_retry(
            state, first, attempt_in_run=1, max_attempts=2
        )
        second = policy.execute_once(state, "search_attractions", {"value": 1})

        self.assertTrue(decision.should_retry)
        self.assertTrue(second.success)
        self.assertEqual(state.tool_call_count, 2)

    def test_llm_invalid_output_enters_existing_output_validation(self):
        injector = FaultInjector(
            [
                FaultRule(
                    target="generate_plan",
                    mode=FaultMode.INVALID_OUTPUT,
                    injected_output={"unexpected": True},
                )
            ]
        )
        registry = ToolRegistry(call_injector=injector)
        registry.register(
            ToolDefinition(
                name="generate_plan",
                description="test",
                input_model=EchoInput,
                output_model=EchoOutput,
                invalid_output_retryable=True,
                handler=lambda item: {"value": item.value},
            )
        )

        result = registry.execute("generate_plan", {"value": 1})

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, ToolErrorType.INVALID_OUTPUT)
        self.assertTrue(result.retryable)

    def test_rate_limit_preserves_provider_diagnostics(self):
        injector = FaultInjector(
            [FaultRule(target="search_hotels", mode=FaultMode.RATE_LIMIT)]
        )
        registry = ToolRegistry(call_injector=injector)
        registry.register(
            ToolDefinition(
                name="search_hotels",
                description="test",
                input_model=EchoInput,
                handler=lambda item: {"value": item.value},
            )
        )

        result = registry.execute("search_hotels", {"value": 1})

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, ToolErrorType.RATE_LIMIT)
        self.assertEqual(result.provider_code, "429")
        self.assertTrue(result.retryable)

    def test_sqlite_proxy_fails_once_without_changing_real_store(self):
        injector = FaultInjector(
            [
                FaultRule(
                    target="sqlite.save_state",
                    mode=FaultMode.SQLITE_LOCKED,
                    call_numbers=[1],
                )
            ]
        )
        state = AgentState.create(
            TripRequest(
                city="杭州",
                start_date="2026-10-12",
                end_date="2026-10-12",
                travel_days=1,
                transportation="公共交通",
                accommodation="经济型酒店",
                preferences=["休闲"],
            ),
            session_id="sqlite-fault-fixture",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteAgentStateStore(Path(temp_dir) / "fault.db")
            proxy = FaultInjectingProxy(store, injector, prefix="sqlite")
            with self.assertRaisesRegex(Exception, "database is locked"):
                proxy.save_state(state)
            proxy.save_state(state)
            restored = store.get_state(state.session_id)

        self.assertEqual(restored.session_id, state.session_id)
        self.assertEqual(injector.call_counts["sqlite.save_state"], 2)


    def test_fault_rule_rejects_non_positive_call_numbers(self):
        with self.assertRaisesRegex(ValueError, "greater than or equal to 1"):
            FaultRule(
                target="search_attractions",
                mode=FaultMode.TIMEOUT,
                call_numbers=[0],
            )

    def test_fault_rule_normalizes_duplicate_call_numbers(self):
        rule = FaultRule(
            target="search_attractions",
            mode=FaultMode.TIMEOUT,
            call_numbers=[3, 1, 3],
        )

        self.assertEqual(rule.call_numbers, [1, 3])


if __name__ == "__main__":
    unittest.main()
