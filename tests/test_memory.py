import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_runtime import AgentAction, AgentActionError, AgentState, TripOrchestrator
from app.memory import SessionNotFoundError, SQLiteAgentStateStore
from app.schemas.trip_schema import TripRequest


def make_request() -> TripRequest:
    return TripRequest(
        city="成都",
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["美食"],
    )


def make_plan() -> dict:
    return {
        "city": "\u6210\u90fd",
        "start_date": "2026-08-10",
        "end_date": "2026-08-11",
        "days": [
            {
                "date": "2026-08-10",
                "day_index": 0,
                "description": "day one",
                "transportation": "public transit",
                "accommodation": "budget hotel",
                "attractions": [],
                "meals": [],
            },
            {
                "date": "2026-08-11",
                "day_index": 1,
                "description": "day two",
                "transportation": "public transit",
                "accommodation": "budget hotel",
                "attractions": [],
                "meals": [],
            },
        ],
        "weather_info": [],
        "overall_suggestions": "book ahead",
        "budget": None,
    }


class AttractionAgent:
    def __init__(self, calls, responses=None):
        self.calls = calls
        self.responses = list(responses or [{"pois": []}])

    def search_attractions(self, city, preferences):
        self.calls.append(AgentAction.SEARCH_ATTRACTIONS)
        return self.responses.pop(0)


class WeatherAgent:
    def __init__(self, calls):
        self.calls = calls

    def get_city_weather(self, city):
        self.calls.append(AgentAction.GET_WEATHER)
        return {"forecasts": []}


class HotelAgent:
    def __init__(self, calls):
        self.calls = calls

    def search_hotels(self, city):
        self.calls.append(AgentAction.SEARCH_HOTELS)
        return {"pois": []}


class PlannerAgent:
    def __init__(self, calls):
        self.calls = calls

    def generate_plan(self, request, attractions, weather, hotels):
        self.calls.append(AgentAction.GENERATE_PLAN)
        return make_plan()

    def repair_plan(
        self,
        request,
        current_plan,
        validation_result,
        attractions,
        weather,
        hotels,
    ):
        self.calls.append(AgentAction.REPAIR_PLAN)
        return make_plan()


def make_orchestrator(store, *, attraction_responses=None):
    calls = []
    return (
        TripOrchestrator(
            attraction_agent=AttractionAgent(calls, attraction_responses),
            weather_agent=WeatherAgent(calls),
            hotel_agent=HotelAgent(calls),
            planner_agent=PlannerAgent(calls),
            max_steps=16,
            max_attempts_per_action=2,
            state_store=store,
        ),
        calls,
    )


class SQLiteAgentStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "sessions.db"
        self.store = SQLiteAgentStateStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_orchestrator_checkpoints_initial_state_and_every_action(self):
        class RecordingStore:
            def __init__(self):
                self.snapshots = []

            def save_state(self, state):
                state.touch()
                self.snapshots.append(state.model_copy(deep=True))

        recording_store = RecordingStore()
        orchestrator, _ = make_orchestrator(recording_store)

        state = orchestrator.run(make_request(), session_id="checkpoint-session")

        # 压缩后仍是“初始状态 + 每个物理步骤”一个检查点，
        # 但最后一个检查点会同时包含多个本地逻辑动作。
        self.assertEqual(len(recording_store.snapshots), state.current_step + 1)
        self.assertEqual(
            [len(item.action_history) for item in recording_store.snapshots],
            [0, 1, 2, 3, 4, 10],
        )
        self.assertEqual(recording_store.snapshots[0].status, "running")
        self.assertEqual(recording_store.snapshots[-1].status, "completed")

    def test_state_round_trip_preserves_history_and_timestamps(self):
        orchestrator, _ = make_orchestrator(self.store)

        state = orchestrator.run(make_request(), session_id="round-trip")
        loaded = self.store.get_state("round-trip")

        self.assertEqual(loaded, state)
        self.assertEqual(loaded.status, "completed")
        self.assertEqual(len(loaded.action_history), 10)
        self.assertEqual(loaded.action_history[-1].action, AgentAction.FINISH)
        self.assertIsNotNone(loaded.created_at.tzinfo)
        self.assertIsNotNone(loaded.updated_at.tzinfo)
        self.assertEqual(loaded.completion_mode, "full")
        self.assertIsNotNone(loaded.acceptance_report)
        self.assertTrue(loaded.acceptance_report.accepted)
        self.assertFalse(loaded.acceptance_report.partial)
        self.assertEqual(loaded.completion_warnings, state.completion_warnings)
        self.assertTrue(loaded.completion_warnings)

    def test_compressed_metadata_survives_sqlite_round_trip(self):
        orchestrator, _ = make_orchestrator(self.store)

        state = orchestrator.run(make_request(), session_id="compressed-round-trip")
        loaded = self.store.get_state("compressed-round-trip")

        self.assertEqual(loaded.local_action_batch_count, state.local_action_batch_count)
        self.assertEqual(
            loaded.compressed_local_action_count,
            state.compressed_local_action_count,
        )
        route_record = loaded.action_history[4]
        compressed_records = [
            record for record in loaded.action_history if record.compressed
        ]
        self.assertEqual(
            route_record.compressed_actions,
            [record.action for record in compressed_records],
        )
        self.assertTrue(
            all(
                record.batch_root_action is AgentAction.ESTIMATE_ROUTES
                for record in compressed_records
            )
        )
        self.assertEqual(
            [record.batch_index for record in compressed_records],
            list(range(1, len(compressed_records) + 1)),
        )
        self.assertEqual(
            [(item.step, item.action) for item in loaded.convergence_history],
            [(item.step, item.action) for item in state.convergence_history],
        )

    def test_list_sessions_returns_summaries_and_status_filter(self):
        complete, _ = make_orchestrator(self.store)
        complete.run(make_request(), session_id="completed-session")

        failed, _ = make_orchestrator(
            self.store,
            attraction_responses=[{"error": "one"}, {"error": "two"}],
        )
        with self.assertRaises(AgentActionError):
            failed.run(make_request(), session_id="failed-session")

        all_sessions = self.store.list_sessions(limit=10)
        failed_sessions = self.store.list_sessions(limit=10, status="failed")

        self.assertEqual({item.session_id for item in all_sessions}, {
            "completed-session",
            "failed-session",
        })
        self.assertEqual([item.session_id for item in failed_sessions], ["failed-session"])
        self.assertEqual(failed_sessions[0].action_count, 2)

    def test_older_checkpoint_without_route_fields_still_loads(self):
        payload = AgentState.create(make_request()).model_dump(mode="json")
        payload["state_version"] = 4
        payload.pop("route_estimates")
        payload.pop("route_plan_fingerprint")
        for field in (
            "route_quality_report",
            "route_quality_plan_fingerprint",
            "route_optimization_count",
            "route_optimization_status",
            "route_optimization_candidate",
            "route_optimization_baseline_plan",
            "route_optimization_baseline_routes",
            "route_optimization_baseline_quality",
            "route_optimization_baseline_fingerprint",
            "route_optimization_history",
            "commute_report",
            "commute_plan_fingerprint",
            "commute_replacement_count",
            "commute_optimization_status",
            "commute_candidate",
            "commute_baseline_plan",
            "commute_baseline_routes",
            "commute_baseline_route_quality",
            "commute_baseline_report",
            "commute_baseline_schedule",
            "commute_baseline_constraint_report",
            "commute_baseline_route_fingerprint",
            "commute_baseline_constraint_fingerprint",
            "commute_excluded_candidate_identities",
            "commute_replacement_history",
            "schedule_quality_report",
            "schedule_quality_plan_fingerprint",
            "schedule_optimization_count",
            "schedule_optimization_status",
            "schedule_optimization_candidate",
            "schedule_optimization_baseline_plan",
            "schedule_optimization_baseline_routes",
            "schedule_optimization_baseline_route_quality",
            "schedule_optimization_baseline_quality",
            "schedule_optimization_baseline_fingerprint",
            "schedule_optimization_history",
            "constraint_report",
            "constraint_plan_fingerprint",
            "constraint_optimization_count",
            "constraint_optimization_status",
            "constraint_optimization_candidate",
            "constraint_optimization_baseline_plan",
            "constraint_optimization_baseline_routes",
            "constraint_optimization_baseline_route_quality",
            "constraint_optimization_baseline_schedule",
            "constraint_optimization_baseline_report",
            "constraint_optimization_baseline_fingerprint",
            "constraint_optimization_history",
            "content_refill_count",
            "content_refill_status",
            "content_refill_candidate",
            "content_refill_baseline_plan",
            "content_refill_baseline_routes",
            "content_refill_baseline_route_quality",
            "content_refill_baseline_schedule",
            "content_refill_baseline_constraint_report",
            "content_refill_baseline_route_fingerprint",
            "content_refill_baseline_constraint_fingerprint",
            "content_refill_excluded_identities",
            "content_refill_history",
            "plan_consistency_fingerprint",
            "plan_consistency_rebuild_count",
        ):
            payload.pop(field)
        payload["execution_budget"].pop("max_route_optimization_attempts")
        payload["execution_budget"].pop("max_schedule_optimization_attempts")
        payload["execution_budget"].pop("max_constraint_optimization_attempts")
        payload["execution_budget"].pop("max_content_refill_attempts")
        payload["execution_budget"].pop("max_commute_replacement_attempts")
        payload["execution_budget"].pop("minimum_total_attractions")

        loaded = AgentState.model_validate(payload)

        self.assertIsNone(loaded.route_estimates)
        self.assertIsNone(loaded.route_plan_fingerprint)
        self.assertIsNone(loaded.route_quality_report)
        self.assertEqual(loaded.route_optimization_count, 0)
        self.assertEqual(loaded.route_optimization_status, "not_started")
        self.assertEqual(
            loaded.execution_budget.max_route_optimization_attempts,
            1,
        )
        self.assertIsNone(loaded.schedule_quality_report)
        self.assertEqual(loaded.schedule_optimization_count, 0)
        self.assertEqual(loaded.schedule_optimization_status, "not_started")
        self.assertEqual(
            loaded.execution_budget.max_schedule_optimization_attempts,
            1,
        )
        self.assertIsNone(loaded.constraint_report)
        self.assertEqual(loaded.constraint_optimization_count, 0)
        self.assertEqual(loaded.constraint_optimization_status, "not_started")
        self.assertEqual(
            loaded.execution_budget.max_constraint_optimization_attempts,
            1,
        )
        self.assertEqual(loaded.execution_budget.max_content_refill_attempts, 2)
        self.assertEqual(loaded.execution_budget.max_commute_replacement_attempts, 2)
        self.assertEqual(loaded.commute_optimization_status, "not_started")
        self.assertEqual(loaded.execution_budget.minimum_total_attractions, 0)
        self.assertEqual(loaded.content_refill_status, "not_started")
        self.assertIsNone(loaded.plan_consistency_fingerprint)

    def test_missing_session_raises_specific_error(self):
        with self.assertRaises(SessionNotFoundError):
            self.store.get_state("missing")

    def test_resume_skips_actions_already_present_in_checkpoint(self):
        partial = AgentState.create(
            make_request(),
            max_steps=9,
            session_id="partial-session",
        )
        partial.status = "running"
        partial.attractions = {"pois": [{"name": "武侯祠"}]}
        self.store.save_state(partial)

        orchestrator, calls = make_orchestrator(self.store)
        resumed = orchestrator.resume(self.store.get_state("partial-session"))

        self.assertEqual(
            calls,
            [
                AgentAction.GET_WEATHER,
                AgentAction.SEARCH_HOTELS,
                AgentAction.GENERATE_PLAN,
            ],
        )
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(self.store.get_state("partial-session").status, "completed")

    def test_failed_session_can_resume_with_fresh_retry_budget(self):
        failing, _ = make_orchestrator(
            self.store,
            attraction_responses=[{"error": "one"}, {"error": "two"}],
        )
        with self.assertRaises(AgentActionError):
            failing.run(make_request(), session_id="retry-session")

        failed_state = self.store.get_state("retry-session")
        self.assertEqual(failed_state.status, "failed")
        self.assertEqual(failed_state.attempts_by_action["search_attractions"], 2)

        succeeding, calls = make_orchestrator(
            self.store,
            attraction_responses=[{"pois": []}],
        )
        resumed = succeeding.resume(failed_state)

        self.assertEqual(calls[0], AgentAction.SEARCH_ATTRACTIONS)
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(resumed.attempts_by_action["search_attractions"], 3)
        self.assertEqual(resumed.action_history[2].attempt, 3)
        self.assertTrue(resumed.action_history[2].success)

    def test_session_http_endpoints_support_query_and_idempotent_resume(self):
        import main

        orchestrator, _ = make_orchestrator(self.store)
        state = orchestrator.run(make_request(), session_id="api-session")

        original_store = main.agent_state_store
        original_orchestrator = main.trip_orchestrator
        main.agent_state_store = self.store
        main.trip_orchestrator = orchestrator
        try:
            client = TestClient(main.app)
            detail_response = client.get("/api/trip/sessions/api-session")
            list_response = client.get("/api/trip/sessions?status=completed")
            resume_response = client.post("/api/trip/sessions/api-session/resume")
            missing_response = client.get("/api/trip/sessions/missing")
        finally:
            main.agent_state_store = original_store
            main.trip_orchestrator = original_orchestrator

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["session_id"], state.session_id)
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()[0]["session_id"], state.session_id)
        self.assertEqual(resume_response.status_code, 200)
        self.assertEqual(resume_response.json()["status"], "completed")
        self.assertEqual(missing_response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
