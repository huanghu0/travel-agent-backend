import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_runtime import AgentState
from app.agent_runtime.acceptance import PartialAcceptanceReport, PlanQualityLevel
from app.commute import CommuteConstraintReport
from app.constraints import TripConstraintReport
from app.evaluation import (
    FIXED_ACCEPTANCE_SCENARIOS,
    build_fixed_acceptance_baseline,
    evaluate_acceptance_case,
)
from app.memory import SQLiteAgentStateStore
from app.routing import RouteQualityReport
from app.scheduling import ScheduleQualityReport
from app.schemas.trip_schema import Attraction, DayPlan, Location, TripPlan


def make_completed_state(scenario_index: int = 0) -> AgentState:
    scenario = FIXED_ACCEPTANCE_SCENARIOS[scenario_index]
    request = scenario.request
    state = AgentState.create(request, session_id=f"acceptance-{scenario.case_id}")
    start_date = date.fromisoformat(request.start_date)
    days = []
    for day_index in range(request.travel_days):
        days.append(
            DayPlan(
                date=(start_date + timedelta(days=day_index)).isoformat(),
                day_index=day_index,
                description="固定验收日程",
                transportation=request.transportation,
                accommodation=request.accommodation,
                attractions=[
                    Attraction(
                        name=f"验收景点{day_index + 1}",
                        address="验收地址",
                        location=Location(longitude=120.1, latitude=30.2),
                        visit_duration=120,
                        description="用于固定基线的景点",
                    )
                ],
            )
        )
    state.trip_plan = TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        overall_suggestions="按时间轴执行并关注天气变化",
    )
    fingerprint = "fixed-baseline-fingerprint"
    state.route_quality_report = RouteQualityReport(
        plan_fingerprint=fingerprint, quality_score=95.0
    )
    state.commute_report = CommuteConstraintReport(plan_fingerprint=fingerprint)
    state.schedule_quality_report = ScheduleQualityReport(
        plan_fingerprint=fingerprint,
        feasible_days=request.travel_days,
        quality_score=94.0,
    )
    state.constraint_report = TripConstraintReport(
        plan_fingerprint=fingerprint, quality_score=96.0
    )
    state.acceptance_report = PartialAcceptanceReport(
        accepted=True,
        partial=False,
        quality_level=PlanQualityLevel.EXCELLENT,
        quality_score=95.0,
        reason="固定验收通过",
    )
    state.completion_mode = "full"
    state.status = "completed"
    state.finished = True
    state.current_step = 12
    state.llm_call_count = 1
    return state


class FixedAcceptanceBaselineTests(unittest.TestCase):
    def test_fixed_suite_covers_five_cities_three_durations_and_transports(self):
        self.assertEqual(len(FIXED_ACCEPTANCE_SCENARIOS), 15)
        self.assertEqual(
            {item.request.city for item in FIXED_ACCEPTANCE_SCENARIOS},
            {"杭州", "北京", "上海", "成都", "西安"},
        )
        self.assertEqual(
            {item.request.travel_days for item in FIXED_ACCEPTANCE_SCENARIOS},
            {1, 3, 5},
        )
        self.assertEqual(
            {item.request.transportation for item in FIXED_ACCEPTANCE_SCENARIOS},
            {"步行", "公共交通", "驾车"},
        )

    def test_good_state_passes_and_missing_cases_remain_visible(self):
        state = make_completed_state()
        result = evaluate_acceptance_case(FIXED_ACCEPTANCE_SCENARIOS[0], state)
        report = build_fixed_acceptance_baseline(
            [state], requested_limit=100, sampled_session_count=1
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.failed_check_codes, [])
        self.assertEqual(report.covered_case_count, 1)
        self.assertEqual(report.passed_case_count, 1)
        self.assertEqual(report.missing_case_count, 14)
        self.assertEqual(report.coverage_rate, round(1 / 15, 4))

    def test_deterministic_quality_gates_detect_core_failures(self):
        state = make_completed_state()
        state.route_quality_report.unavailable_legs = 1
        state.commute_report.excessive_segment_count = 1
        state.schedule_quality_report.total_overtime_minutes = 120
        state.constraint_report.error_count = 1

        result = evaluate_acceptance_case(FIXED_ACCEPTANCE_SCENARIOS[0], state)

        self.assertEqual(result.status, "failed")
        self.assertIn("route.available", result.failed_check_codes)
        self.assertIn("commute.segment_limit", result.failed_check_codes)
        self.assertIn("schedule.overtime", result.failed_check_codes)
        self.assertIn("constraints.errors", result.failed_check_codes)

    def test_cli_list_starts_without_circular_import(self):
        project_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "run_fixed_acceptance_baseline.py"),
                "--list",
            ],
            cwd=project_root,
            capture_output=True,
            check=False,
        )

        stderr = result.stderr.decode(errors="replace")
        self.assertEqual(result.returncode, 0, msg=stderr)
        self.assertEqual(len(result.stdout.strip().splitlines()), 15)
        self.assertIn(b"hangzhou-1d-walking", result.stdout)

    def test_sqlite_and_http_endpoints_return_fixed_baseline(self):
        import main

        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteAgentStateStore(Path(temp_dir) / "acceptance.db")
            store.save_state(make_completed_state())

            original_store = main.agent_state_store
            main.agent_state_store = store
            try:
                client = TestClient(main.app)
                scenario_response = client.get(
                    "/api/agent/analytics/fixed-acceptance-scenarios"
                )
                report_response = client.get(
                    "/api/agent/analytics/fixed-acceptance-baseline"
                )
            finally:
                main.agent_state_store = original_store

        self.assertEqual(scenario_response.status_code, 200)
        self.assertEqual(len(scenario_response.json()), 15)
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(report_response.json()["covered_case_count"], 1)
        self.assertEqual(report_response.json()["passed_case_count"], 1)


if __name__ == "__main__":
    unittest.main()
