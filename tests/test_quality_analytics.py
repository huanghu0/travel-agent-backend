import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_runtime import ActionRecord, AgentAction, AgentState
from app.agent_runtime.acceptance import PartialAcceptanceReport, PlanQualityLevel
from app.memory import SQLiteAgentStateStore
from app.schemas.trip_schema import TripRequest


def make_state(
    session_id: str,
    *,
    city: str,
    status: str,
    completion_mode: str | None = None,
    quality_level: PlanQualityLevel | None = None,
    quality_score: float | None = None,
    issue_codes: list[str] | None = None,
) -> AgentState:
    state = AgentState.create(
        TripRequest(
            city=city,
            start_date="2026-10-12",
            end_date="2026-10-14",
            travel_days=3,
            transportation="公共交通",
            accommodation="经济型酒店",
            preferences=["休闲"],
        ),
        session_id=session_id,
    )
    state.status = status
    state.finished = status == "completed"
    state.current_step = 8
    state.tool_call_count = 4
    state.llm_call_count = 1
    state.total_duration_ms = 1200
    state.action_history = [
        ActionRecord(
            step=1,
            action=AgentAction.SEARCH_ATTRACTIONS,
            reason="test",
            success=True,
        ),
        ActionRecord(
            step=2,
            action=AgentAction.GENERATE_PLAN,
            reason="test",
            success=True,
        ),
    ]
    state.completion_mode = completion_mode
    if quality_level is not None and quality_score is not None:
        state.acceptance_report = PartialAcceptanceReport(
            accepted=status == "completed",
            partial=completion_mode == "partial",
            quality_level=quality_level,
            quality_score=quality_score,
            warnings=["存在非关键问题"] if issue_codes else [],
            unresolved_issue_codes=issue_codes or [],
        )
        state.completion_warnings = state.acceptance_report.warnings
    return state


class QualityAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "quality.db"
        self.store = SQLiteAgentStateStore(self.db_path)
        self.store.save_state(
            make_state(
                "full",
                city="杭州",
                status="completed",
                completion_mode="full",
                quality_level=PlanQualityLevel.EXCELLENT,
                quality_score=96.0,
            )
        )
        self.store.save_state(
            make_state(
                "partial",
                city="杭州",
                status="completed",
                completion_mode="partial",
                quality_level=PlanQualityLevel.ACCEPTABLE,
                quality_score=82.0,
                issue_codes=["schedule.daily_overtime"],
            )
        )
        self.store.save_state(
            make_state(
                "failed",
                city="成都",
                status="failed",
                quality_level=PlanQualityLevel.DEGRADED,
                quality_score=55.0,
                issue_codes=["route.unavailable"],
            )
        )
        self.store.save_state(make_state("running", city="成都", status="running"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_quality_baseline_aggregates_completion_quality_and_cost(self):
        report = self.store.get_quality_baseline(limit=100)

        self.assertEqual(report.overall.session_count, 4)
        self.assertEqual(report.overall.full_completed_count, 1)
        self.assertEqual(report.overall.partial_completed_count, 1)
        self.assertEqual(report.overall.failed_count, 1)
        self.assertEqual(report.overall.in_progress_count, 1)
        self.assertEqual(report.overall.success_rate, 0.5)
        self.assertEqual(report.overall.avg_quality_score, 77.67)
        self.assertEqual(report.overall.avg_tool_calls, 4.0)
        self.assertEqual(report.overall.avg_llm_calls, 1.0)
        self.assertEqual(report.overall.estimated_avoided_llm_repair_calls, 1)

        cities = {item.value: item for item in report.cities}
        self.assertEqual(cities["杭州"].success_rate, 1.0)
        self.assertEqual(cities["成都"].failure_rate, 0.5)
        issues = {item.issue_code: item for item in report.common_issue_codes}
        self.assertEqual(issues["schedule.daily_overtime"].partial_session_count, 1)
        self.assertEqual(issues["route.unavailable"].session_count, 1)

    def test_quality_filters_and_denormalized_columns(self):
        report = self.store.get_quality_baseline(
            city=" 杭州 ", completion_mode="partial", transportation="公共交通"
        )
        self.assertEqual(report.matching_session_count, 1)
        self.assertEqual(report.overall.partial_completed_count, 1)
        self.assertEqual(report.city_filter, "杭州")

        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                """
                SELECT travel_days, transportation, completion_mode, quality_level,
                       quality_score, warning_count, issue_codes_json, tool_call_count,
                       llm_call_count
                FROM agent_sessions WHERE session_id = 'partial'
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row[0], 3)
        self.assertEqual(row[1], "公共交通")
        self.assertEqual(row[2], "partial")
        self.assertEqual(row[3], "acceptable")
        self.assertEqual(row[4], 82.0)
        self.assertEqual(row[5], 1)
        self.assertIn("schedule.daily_overtime", row[6])
        self.assertEqual(row[7], 4)
        self.assertEqual(row[8], 1)

    def test_http_endpoint_returns_quality_baseline(self):
        import main

        original_store = main.agent_state_store
        main.agent_state_store = self.store
        try:
            response = TestClient(main.app).get(
                "/api/agent/analytics/quality-baseline",
                params={"city": "杭州", "top_n": 5},
            )
        finally:
            main.agent_state_store = original_store

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["overall"]["session_count"], 2)
        self.assertEqual(payload["overall"]["partial_completed_count"], 1)
        self.assertIn("common_issue_codes", payload)


if __name__ == "__main__":
    unittest.main()
