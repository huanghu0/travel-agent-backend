import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_runtime import AgentState, PartialAcceptanceReport, PlanQualityLevel
from app.commute.models import CommuteConstraintReport
from app.constraints.models import TripConstraintReport
from app.memory import SQLiteAgentStateStore
from app.routing.quality import RouteQualityReport
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling.models import DayScheduleQuality, ScheduleQualityReport, TimelineItem


class ExecutionViewApiTests(unittest.TestCase):
    """验证结果页轻量接口保留展示数据，同时裁掉智能体内部状态。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = SQLiteAgentStateStore(Path(self.temp_dir.name) / "sessions.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _build_state(self) -> AgentState:
        request = TripRequest(
            city="杭州",
            start_date="2026-08-20",
            end_date="2026-08-20",
            travel_days=1,
            transportation="公共交通",
            accommodation="经济型酒店",
            preferences=["自然风光"],
        )
        plan = TripPlan.model_validate(
            {
                "city": "杭州",
                "start_date": "2026-08-20",
                "end_date": "2026-08-20",
                "days": [
                    {
                        "date": "2026-08-20",
                        "day_index": 0,
                        "description": "西湖一日游",
                        "transportation": "公共交通",
                        "accommodation": "湖滨酒店",
                        "hotel": {
                            "name": "湖滨酒店",
                            "address": "湖滨路 1 号",
                            "price_range": "300-400 元",
                            "rating": "4.6",
                            "distance": "1 公里",
                            "type": "经济型酒店",
                        },
                        "attractions": [],
                        "meals": [
                            {
                                "type": "lunch",
                                "name": "西湖餐厅",
                                "opening_hours": "10:30-21:00",
                                "planned_start_time": "12:00",
                                "planned_end_time": "13:00",
                                "opening_status": "open",
                                "source": "amap",
                            }
                        ],
                    }
                ],
                "weather_info": [],
                "overall_suggestions": "错峰出行",
            }
        )
        state = AgentState.create(request, session_id="execution-view-session")
        state.status = "completed"
        state.finished = True
        state.current_step = 8
        state.trip_plan = plan
        state.route_estimates = {
            "provider": "amap",
            "requested_legs": 2,
            "evaluated_legs": 2,
            "truncated_legs": 0,
            "cache_hits": 1,
            "cache_misses": 1,
            "failed_legs": 0,
            "routes": [
                {
                    "provider": "amap",
                    "day_index": 0,
                    "leg_index": 0,
                    "leg_type": "hotel_departure",
                    "date": "2026-08-20",
                    "origin_name": "湖滨酒店",
                    "destination_name": "西湖",
                    "mode": "transit",
                    "available": True,
                    "distance_meters": 2300,
                    "duration_seconds": 960,
                    "cache_hit": True,
                },
                {
                    "provider": "amap",
                    "day_index": 0,
                    "leg_index": 0,
                    "leg_type": "hotel_return",
                    "date": "2026-08-20",
                    "origin_name": "西湖",
                    "destination_name": "湖滨酒店",
                    "mode": "transit",
                    "available": True,
                    "distance_meters": 2400,
                    "duration_seconds": 1020,
                    "cache_hit": False,
                },
            ],
        }
        state.route_quality_report = RouteQualityReport(
            plan_fingerprint="plan-fp",
            total_legs=2,
            available_legs=2,
            total_distance_meters=4700,
            total_duration_seconds=1980,
            quality_score=92,
        )
        state.schedule_quality_report = ScheduleQualityReport(
            plan_fingerprint="plan-fp",
            feasible_days=1,
            total_transportation_minutes=33,
            quality_score=95,
            days=[
                DayScheduleQuality(
                    day_index=0,
                    date="2026-08-20",
                    feasible=True,
                    timeline=[
                        TimelineItem(
                            item_type="transportation",
                            name="湖滨酒店 → 西湖",
                            start_time="08:30",
                            end_time="08:46",
                            duration_minutes=16,
                            day_index=0,
                            transportation_time_source="amap",
                        ),
                        TimelineItem(
                            item_type="meal",
                            name="西湖餐厅",
                            start_time="12:00",
                            end_time="13:00",
                            duration_minutes=60,
                            day_index=0,
                        ),
                    ],
                )
            ],
        )
        state.commute_report = CommuteConstraintReport(
            plan_fingerprint="plan-fp", total_segments=2, max_duration_seconds=1020
        )
        state.constraint_report = TripConstraintReport(
            plan_fingerprint="plan-fp", quality_score=96, feasible=True
        )
        state.acceptance_report = PartialAcceptanceReport(
            accepted=True,
            quality_level=PlanQualityLevel.EXCELLENT,
            quality_score=94.5,
            warnings=["午餐高峰可能排队"],
        )
        state.completion_mode = "full"
        state.completion_warnings = ["午餐高峰可能排队"]
        state.attractions = {"pois": [{"name": "不应返回的候选景点"}]}
        return state

    def test_execution_view_preserves_executable_trip_data_and_trims_agent_state(self):
        import main

        state = self._build_state()
        self.store.save_state(state)
        original_store = main.agent_state_store
        main.agent_state_store = self.store
        try:
            response = TestClient(main.app).get(
                "/api/trip/sessions/execution-view-session/execution-view"
            )
        finally:
            main.agent_state_store = original_store

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["quality_level"], "excellent")
        self.assertEqual(body["route_summary"]["evaluated_legs"], 2)
        self.assertEqual(
            [item["leg_type"] for item in body["route_segments"]],
            ["hotel_departure", "hotel_return"],
        )
        self.assertEqual(
            body["schedule_quality_report"]["days"][0]["timeline"][0][
                "transportation_time_source"
            ],
            "amap",
        )
        self.assertEqual(
            body["trip_plan"]["days"][0]["meals"][0]["opening_status"],
            "open",
        )
        self.assertTrue(body["constraint_report"]["feasible"])
        self.assertNotIn("action_history", body)
        self.assertNotIn("attractions", body)
        self.assertNotIn("route_optimization_candidate", body)

    def test_execution_view_handles_missing_routes_and_unknown_session(self):
        import main

        state = AgentState.create(
            TripRequest(
                city="成都",
                start_date="2026-08-20",
                end_date="2026-08-20",
                travel_days=1,
                transportation="步行",
                accommodation="民宿",
                preferences=[],
            ),
            session_id="empty-execution-view",
        )
        self.store.save_state(state)
        original_store = main.agent_state_store
        main.agent_state_store = self.store
        try:
            client = TestClient(main.app)
            response = client.get(
                "/api/trip/sessions/empty-execution-view/execution-view"
            )
            missing = client.get("/api/trip/sessions/missing/execution-view")
        finally:
            main.agent_state_store = original_store

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["route_segments"], [])
        self.assertIsNone(response.json()["trip_plan"])
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
