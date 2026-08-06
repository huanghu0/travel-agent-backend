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
            max_steps=10,
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

        orchestrator.run(make_request(), session_id="checkpoint-session")

        self.assertEqual(
            [len(item.action_history) for item in recording_store.snapshots],
            [0, 1, 2, 3, 4, 5, 6, 7],
        )
        self.assertEqual(recording_store.snapshots[0].status, "running")
        self.assertEqual(recording_store.snapshots[-1].status, "completed")
    def test_state_round_trip_preserves_history_and_timestamps(self):
        orchestrator, _ = make_orchestrator(self.store)

        state = orchestrator.run(make_request(), session_id="round-trip")
        loaded = self.store.get_state("round-trip")

        self.assertEqual(loaded, state)
        self.assertEqual(loaded.status, "completed")
        self.assertEqual(len(loaded.action_history), 7)
        self.assertEqual(loaded.action_history[-1].action, AgentAction.FINISH)
        self.assertIsNotNone(loaded.created_at.tzinfo)
        self.assertIsNotNone(loaded.updated_at.tzinfo)

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

        loaded = AgentState.model_validate(payload)

        self.assertIsNone(loaded.route_estimates)
        self.assertIsNone(loaded.route_plan_fingerprint)

    def test_missing_session_raises_specific_error(self):
        with self.assertRaises(SessionNotFoundError):
            self.store.get_state("missing")

    def test_resume_skips_actions_already_present_in_checkpoint(self):
        partial = AgentState.create(
            make_request(),
            max_steps=8,
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
