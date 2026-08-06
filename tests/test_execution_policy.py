import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from app.agent_runtime import (
    AgentBudgetExceededError,
    AgentState,
    CircuitBreaker,
    CircuitState,
    ExecutionPolicy,
    TripOrchestrator,
)
from app.memory import SQLiteAgentStateStore
from app.schemas.trip_schema import TripRequest
from app.tools.models import ToolErrorType
from app.tools.registry import ToolDefinition, ToolRegistry, ToolResultError


class InputModel(BaseModel):
    value: str = Field(min_length=1)


def make_request() -> TripRequest:
    return TripRequest(
        city="成都",
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史"],
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
                "description": "第一天",
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "attractions": [],
                "meals": [],
            },
            {
                "date": "2026-08-11",
                "day_index": 1,
                "description": "第二天",
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "attractions": [],
                "meals": [],
            },
        ],
        "weather_info": [],
        "overall_suggestions": "提前预约。",
        "budget": None,
    }


class SimpleAttractionAgent:
    def __init__(self, calls):
        self.calls = calls

    def search_attractions(self, city, preferences):
        self.calls.append("search_attractions")
        return {"pois": []}


class SimpleWeatherAgent:
    def __init__(self, calls):
        self.calls = calls

    def get_city_weather(self, city):
        self.calls.append("get_weather")
        return {"forecasts": []}


class SimpleHotelAgent:
    def __init__(self, calls):
        self.calls = calls

    def search_hotels(self, city):
        self.calls.append("search_hotels")
        return {"pois": []}


class SimplePlannerAgent:
    def __init__(self, calls):
        self.calls = calls

    def generate_plan(self, request, attractions, weather, hotels):
        self.calls.append("generate_plan")
        return make_plan()

    def repair_plan(self, *args):
        self.calls.append("repair_plan")
        return make_plan()


def make_budget_orchestrator(calls, **kwargs):
    return TripOrchestrator(
        attraction_agent=SimpleAttractionAgent(calls),
        weather_agent=SimpleWeatherAgent(calls),
        hotel_agent=SimpleHotelAgent(calls),
        planner_agent=SimplePlannerAgent(calls),
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
        retry_jitter_seconds=0,
        **kwargs,
    )


class ExecutionBudgetTests(unittest.TestCase):
    def test_tool_call_budget_stops_before_next_handler(self):
        calls = []
        orchestrator = make_budget_orchestrator(
            calls,
            max_tool_calls=1,
            max_llm_calls=10,
        )

        with self.assertRaises(AgentBudgetExceededError) as caught:
            orchestrator.run(make_request())

        state = caught.exception.state
        self.assertEqual(calls, ["search_attractions"])
        self.assertEqual(state.tool_call_count, 1)
        self.assertEqual(state.llm_call_count, 1)
        self.assertEqual(state.status, "budget_exhausted")
        self.assertIn("最大工具调用次数 1", state.budget_exhausted_reason)
        self.assertEqual(state.last_action_result.error_type, ToolErrorType.BUDGET_EXCEEDED)

    def test_llm_call_budget_stops_before_next_llm_tool(self):
        calls = []
        orchestrator = make_budget_orchestrator(
            calls,
            max_tool_calls=10,
            max_llm_calls=1,
        )

        with self.assertRaises(AgentBudgetExceededError) as caught:
            orchestrator.run(make_request())

        state = caught.exception.state
        self.assertEqual(calls, ["search_attractions"])
        self.assertEqual(state.llm_call_count, 1)
        self.assertIn("最大 LLM 调用次数 1", state.budget_exhausted_reason)

    def test_expired_deadline_stops_without_executing_tool(self):
        calls = []
        orchestrator = make_budget_orchestrator(calls)
        state = AgentState.create(make_request())
        state.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)

        with self.assertRaises(AgentBudgetExceededError):
            orchestrator.resume(state)

        self.assertEqual(calls, [])
        self.assertEqual(state.tool_call_count, 0)
        self.assertEqual(state.status, "budget_exhausted")

    def test_sqlite_round_trip_and_resume_do_not_reset_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteAgentStateStore(Path(temp_dir) / "memory.db")
            calls = []
            first = make_budget_orchestrator(
                calls,
                max_tool_calls=1,
                max_llm_calls=10,
                state_store=store,
            )
            with self.assertRaises(AgentBudgetExceededError) as caught:
                first.run(make_request(), session_id="budget-session")

            loaded = store.get_state("budget-session")
            self.assertEqual(loaded.tool_call_count, 1)
            self.assertEqual(
                loaded.budget_exhausted_reason,
                caught.exception.state.budget_exhausted_reason,
            )

            second_calls = []
            second = make_budget_orchestrator(
                second_calls,
                max_tool_calls=99,
                max_llm_calls=99,
                state_store=store,
            )
            with self.assertRaises(AgentBudgetExceededError):
                second.resume(loaded)

            self.assertEqual(second_calls, [])
            self.assertEqual(loaded.execution_budget.max_tool_calls, 1)
            self.assertEqual(loaded.tool_call_count, 1)


class RetryAndCircuitBreakerTests(unittest.TestCase):
    def test_exponential_backoff_is_the_only_retry_scheduler(self):
        outcomes = ["fail", "fail", "success"]
        handler_calls = []
        sleep_calls = []
        registry = ToolRegistry()

        def handler(value):
            handler_calls.append(value.value)
            outcome = outcomes.pop(0)
            if outcome == "fail":
                raise ToolResultError(
                    "temporary",
                    error_type=ToolErrorType.UPSTREAM,
                    retryable=True,
                )
            return {"ok": True}

        registry.register(
            ToolDefinition(
                name="unstable",
                description="unstable",
                input_model=InputModel,
                handler=handler,
                llm_call_cost=1,
            )
        )
        policy = ExecutionPolicy(
            registry,
            retry_base_delay_seconds=1,
            retry_max_delay_seconds=10,
            retry_jitter_seconds=0,
            circuit_breaker=CircuitBreaker(failure_threshold=10),
            sleeper=sleep_calls.append,
        )
        state = AgentState.create(make_request(), max_llm_calls=10)

        for attempt in range(1, 4):
            result = policy.execute_once(state, "unstable", {"value": "x"})
            if result.success:
                break
            decision = policy.decide_retry(
                state,
                result,
                attempt_in_run=attempt,
                max_attempts=3,
            )
            self.assertTrue(decision.should_retry)
            policy.sleep_before_retry(decision.delay_seconds)

        self.assertTrue(result.success)
        self.assertEqual(handler_calls, ["x", "x", "x"])
        self.assertEqual(sleep_calls, [1, 2])
        self.assertEqual(state.tool_call_count, 3)
        self.assertEqual(state.llm_call_count, 3)

    def test_circuit_opens_then_half_open_success_closes_it(self):
        monotonic = [0.0]
        should_fail = [True]
        handler_calls = []
        registry = ToolRegistry()

        def handler(value):
            handler_calls.append(value.value)
            if should_fail[0]:
                raise ToolResultError(
                    "upstream down",
                    error_type=ToolErrorType.UPSTREAM,
                    retryable=True,
                )
            return {"ok": True}

        registry.register(
            ToolDefinition(
                name="service",
                description="service",
                input_model=InputModel,
                handler=handler,
            )
        )
        breaker = CircuitBreaker(
            failure_threshold=2,
            recovery_timeout_seconds=5,
            clock=lambda: monotonic[0],
        )
        policy = ExecutionPolicy(registry, circuit_breaker=breaker)
        state = AgentState.create(make_request(), max_tool_calls=10)

        first = policy.execute_once(state, "service", {"value": "a"})
        second = policy.execute_once(state, "service", {"value": "b"})
        blocked = policy.execute_once(state, "service", {"value": "c"})

        self.assertFalse(first.success)
        self.assertFalse(second.success)
        self.assertEqual(breaker.state_for("service"), CircuitState.OPEN)
        self.assertEqual(blocked.error_type, ToolErrorType.CIRCUIT_OPEN)
        self.assertEqual(handler_calls, ["a", "b"])
        self.assertEqual(state.tool_call_count, 2)

        monotonic[0] = 6.0
        should_fail[0] = False
        recovered = policy.execute_once(state, "service", {"value": "d"})

        self.assertTrue(recovered.success)
        self.assertEqual(recovered.circuit_state, CircuitState.CLOSED.value)
        self.assertEqual(breaker.state_for("service"), CircuitState.CLOSED)
        self.assertEqual(handler_calls, ["a", "b", "d"])


if __name__ == "__main__":
    unittest.main()
