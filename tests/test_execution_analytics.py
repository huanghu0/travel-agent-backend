import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_runtime import ActionRecord, AgentAction, AgentState
from app.memory import SQLiteAgentStateStore
from app.schemas.trip_schema import TripRequest
from tests.auth_test_helpers import (
    TEST_USER,
    install_main_auth_override,
    remove_main_auth_override,
)


def make_request(city: str) -> TripRequest:
    return TripRequest(
        city=city,
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["休闲"],
    )


def save_session(
    store: SQLiteAgentStateStore,
    *,
    session_id: str,
    city: str,
    status: str,
    current_step: int,
    records: list[ActionRecord],
) -> AgentState:
    state = AgentState.create(
        make_request(city), session_id=session_id, user_id=TEST_USER.user_id
    )
    state.status = status
    state.finished = status == "completed"
    state.current_step = current_step
    state.action_history = records
    store.save_state(state)
    return state


class ExecutionBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteAgentStateStore(
            Path(self.temp_dir.name) / "execution-baseline.db"
        )
        save_session(
            self.store,
            session_id="hangzhou-completed",
            city="杭州",
            status="completed",
            current_step=2,
            records=[
                ActionRecord(
                    step=1,
                    action=AgentAction.SEARCH_ATTRACTIONS,
                    reason="test",
                    success=True,
                    duration_ms=20,
                ),
                ActionRecord(
                    step=2,
                    action=AgentAction.GET_WEATHER,
                    reason="test",
                    success=True,
                    duration_ms=10,
                ),
                ActionRecord(
                    step=2,
                    action=AgentAction.SEARCH_ATTRACTIONS,
                    reason="test",
                    success=True,
                    compressed=True,
                    batch_root_action=AgentAction.GET_WEATHER,
                    batch_index=1,
                    duration_ms=30,
                ),
            ],
        )
        save_session(
            self.store,
            session_id="hangzhou-failed",
            city="杭州",
            status="failed",
            current_step=2,
            records=[
                ActionRecord(
                    step=1,
                    action=AgentAction.SEARCH_ATTRACTIONS,
                    reason="test",
                    success=False,
                    duration_ms=40,
                ),
                ActionRecord(
                    step=2,
                    action=AgentAction.SEARCH_ATTRACTIONS,
                    reason="test",
                    success=True,
                    duration_ms=50,
                ),
            ],
        )
        save_session(
            self.store,
            session_id="chengdu-completed",
            city="成都",
            status="completed",
            current_step=2,
            records=[
                ActionRecord(
                    step=1,
                    action=AgentAction.SEARCH_ATTRACTIONS,
                    reason="test",
                    success=True,
                    duration_ms=60,
                ),
                ActionRecord(
                    step=2,
                    action=AgentAction.GET_WEATHER,
                    reason="test",
                    success=True,
                    duration_ms=20,
                ),
            ],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_baseline_aggregates_completion_actions_transitions_and_cycles(self):
        report = self.store.get_execution_baseline(limit=100, top_n=10)

        self.assertEqual(report.matching_session_count, 3)
        self.assertEqual(report.analyzed_session_count, 3)
        self.assertFalse(report.truncated)
        self.assertEqual(report.status_counts, {"completed": 2, "failed": 1})
        self.assertEqual(report.overall.completed_session_count, 2)
        self.assertEqual(report.overall.completion_rate, 0.6667)
        self.assertEqual(report.overall.avg_physical_steps, 2.0)
        self.assertEqual(report.overall.avg_logical_actions, 2.33)
        self.assertEqual(report.overall.step_compression_rate, 0.1429)

        cities = {item.city: item for item in report.cities}
        self.assertEqual(cities["杭州"].session_count, 2)
        self.assertEqual(cities["杭州"].completion_rate, 0.5)
        self.assertEqual(cities["成都"].completion_rate, 1.0)

        actions = {item.action: item for item in report.actions}
        attraction_stats = actions[AgentAction.SEARCH_ATTRACTIONS]
        self.assertEqual(attraction_stats.execution_count, 5)
        self.assertEqual(attraction_stats.success_count, 4)
        self.assertEqual(attraction_stats.failure_count, 1)
        self.assertEqual(attraction_stats.compressed_count, 1)
        self.assertEqual(attraction_stats.session_count, 3)

        transitions = {
            (item.from_action, item.to_action): item
            for item in report.common_transitions
        }
        search_to_weather = transitions[
            (AgentAction.SEARCH_ATTRACTIONS, AgentAction.GET_WEATHER)
        ]
        self.assertEqual(search_to_weather.transition_count, 2)
        self.assertEqual(search_to_weather.completed_session_count, 2)
        self.assertEqual(search_to_weather.cross_physical_step_count, 2)
        self.assertEqual(
            transitions[
                (AgentAction.GET_WEATHER, AgentAction.SEARCH_ATTRACTIONS)
            ].same_physical_step_count,
            1,
        )

        cycle_paths = {
            tuple(item.actions): item for item in report.common_cycles
        }
        self.assertIn(
            (
                AgentAction.SEARCH_ATTRACTIONS,
                AgentAction.GET_WEATHER,
                AgentAction.SEARCH_ATTRACTIONS,
            ),
            cycle_paths,
        )
        self.assertIn(
            (AgentAction.SEARCH_ATTRACTIONS, AgentAction.SEARCH_ATTRACTIONS),
            cycle_paths,
        )

    def test_filters_limits_and_cycle_span_are_applied(self):
        city_report = self.store.get_execution_baseline(
            city=" 杭州 ", status="completed", max_cycle_span=1
        )
        limited_report = self.store.get_execution_baseline(limit=2)

        self.assertEqual(city_report.city_filter, "杭州")
        self.assertEqual(city_report.status_filter, "completed")
        self.assertEqual(city_report.matching_session_count, 1)
        self.assertEqual(city_report.overall.completion_rate, 1.0)
        self.assertEqual(city_report.common_cycles, [])
        self.assertEqual(limited_report.matching_session_count, 3)
        self.assertEqual(limited_report.sampled_row_count, 2)
        self.assertTrue(limited_report.truncated)

    def test_http_endpoint_returns_execution_baseline(self):
        import main

        original_store = main.agent_state_store
        main.agent_state_store = self.store
        install_main_auth_override(main)
        try:
            response = TestClient(main.app).get(
                "/api/trip/analytics/execution-baseline",
                params={"city": "杭州", "top_n": 5},
            )
        finally:
            remove_main_auth_override(main)
            main.agent_state_store = original_store

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["city_filter"], "杭州")
        self.assertEqual(payload["matching_session_count"], 2)
        self.assertEqual(payload["overall"]["completion_rate"], 0.5)
        self.assertIn("common_transitions", payload)
        self.assertIn("common_cycles", payload)


if __name__ == "__main__":
    unittest.main()
