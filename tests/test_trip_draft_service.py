import tempfile
import unittest
from pathlib import Path

from app.agent_runtime import AgentState, TripOrchestrator
from app.memory import SQLiteAgentStateStore, SQLiteTripVersionStore
from app.providers.amap.models import RouteEstimateResult
from app.routing import build_route_legs, plan_route_fingerprint
from app.schemas.trip_draft_schema import TripDraftCreate
from app.schemas.trip_schema import TripPlan, TripRequest
from app.services import TripDraftService
from app.tools.registry import ToolDefinition, ToolRegistry
from app.tools.trip_registry import EstimateRoutesInput


class TripDraftServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        database = Path(self.tempdir.name) / "memory.db"
        self.state_store = SQLiteAgentStateStore(database)
        self.version_store = SQLiteTripVersionStore(database)
        self.request = TripRequest(
            city="杭州",
            start_date="2026-08-20",
            end_date="2026-08-20",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            preferences=["自然风光"],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def _plan() -> TripPlan:
        return TripPlan.model_validate(
            {
                "city": "杭州",
                "start_date": "2026-08-20",
                "end_date": "2026-08-20",
                "days": [
                    {
                        "date": "2026-08-20",
                        "day_index": 0,
                        "description": "杭州一日游",
                        "transportation": "公共交通",
                        "accommodation": "经济型酒店",
                        "attractions": [
                            {"name": "A", "address": "A路", "location": {"longitude": 120.10, "latitude": 30.20}, "visit_duration": 60, "description": "A"},
                            {"name": "B", "address": "B路", "location": {"longitude": 120.11, "latitude": 30.21}, "visit_duration": 60, "description": "B"},
                            {"name": "C", "address": "C路", "location": {"longitude": 120.12, "latitude": 30.22}, "visit_duration": 60, "description": "C"},
                        ],
                        "meals": [],
                    }
                ],
                "weather_info": [],
                "overall_suggestions": "合理安排时间",
                "budget": None,
            }
        )

    def _build_state(self, plan: TripPlan) -> AgentState:
        legs = build_route_legs(self.request, plan)
        routes = [
            {
                "day_index": leg.day_index,
                "leg_index": leg.leg_index,
                "leg_type": leg.leg_type,
                "date": leg.date,
                "origin_name": leg.origin.name,
                "destination_name": leg.destination.name,
                "mode": leg.mode,
                "available": True,
                "distance_meters": 1000,
                "duration_seconds": 600,
            }
            for leg in legs
        ]
        return AgentState(
            request=self.request,
            status="completed",
            finished=True,
            trip_plan=plan,
            route_estimates=RouteEstimateResult(
                plan_fingerprint=plan_route_fingerprint(self.request, plan),
                requested_legs=len(legs),
                evaluated_legs=len(legs),
                routes=routes,
            ).model_dump(mode="json"),
        )

    def test_evaluate_only_queries_changed_route_leg_and_confirm_updates_state(self):
        plan = self._plan()
        state = self._build_state(plan)
        self.state_store.save_state(state)
        queried = []
        registry = ToolRegistry()

        def estimate(value: EstimateRoutesInput):
            queried.extend(value.legs)
            return RouteEstimateResult(
                plan_fingerprint=value.plan_fingerprint,
                requested_legs=len(value.legs),
                evaluated_legs=len(value.legs),
                routes=[
                    {
                        "day_index": leg.day_index,
                        "leg_index": leg.leg_index,
                        "leg_type": leg.leg_type,
                        "date": leg.date,
                        "origin_name": leg.origin.name,
                        "destination_name": leg.destination.name,
                        "mode": leg.mode,
                        "available": True,
                        "distance_meters": 800,
                        "duration_seconds": 480,
                    }
                    for leg in value.legs
                ],
            )

        registry.register(
            ToolDefinition(
                name="estimate_routes",
                description="测试路线",
                input_model=EstimateRoutesInput,
                handler=estimate,
                output_model=RouteEstimateResult,
            )
        )
        service = TripDraftService(
            state_store=self.state_store,
            version_store=self.version_store,
            tool_registry=registry,
            orchestrator=TripOrchestrator(tool_registry=registry),
        )
        edited = plan.model_copy(deep=True)
        edited.days[0].attractions[2].location.longitude = 120.13
        draft = service.create_draft(
            state.session_id, TripDraftCreate(trip_plan=edited)
        )
        result = service.evaluate_draft(state.session_id, draft.draft_id)

        self.assertEqual(result.diff.reused_route_legs, 1)
        self.assertEqual(result.diff.queried_route_legs, 1)
        self.assertEqual(len(queried), 1)
        self.assertEqual(queried[0].origin.name, "B")
        self.assertEqual(queried[0].destination.name, "C")

        confirmed = service.confirm_draft(state.session_id, draft.draft_id)
        saved = self.state_store.get_state(state.session_id)
        self.assertEqual(confirmed.confirmed_version.version_number, 2)
        self.assertEqual(saved.trip_plan.days[0].attractions[2].location.longitude, 120.13)
        self.assertIsNotNone(saved.schedule_quality_report)
        self.assertIsNotNone(saved.constraint_report)

    def test_unchanged_draft_reuses_all_routes_without_tool_call(self):
        plan = self._plan()
        state = self._build_state(plan)
        self.state_store.save_state(state)
        registry = ToolRegistry()
        service = TripDraftService(
            state_store=self.state_store,
            version_store=self.version_store,
            tool_registry=registry,
            orchestrator=TripOrchestrator(tool_registry=registry),
        )
        draft = service.create_draft(
            state.session_id, TripDraftCreate(trip_plan=plan)
        )
        result = service.evaluate_draft(state.session_id, draft.draft_id)
        self.assertEqual(result.diff.reused_route_legs, 2)
        self.assertEqual(result.diff.queried_route_legs, 0)
        self.assertEqual(result.diff.affected_route_keys, [])


if __name__ == "__main__":
    unittest.main()
