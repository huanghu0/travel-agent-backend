import unittest

from app.agent_runtime import (
    AgentState,
    PartialAcceptancePolicy,
    PlanQualityLevel,
    TripValidationResult,
    ValidationIssue,
    ValidationSeverity,
)
from app.commute import CommuteConstraintReport
from app.constraints import TripConstraintReport
from app.routing import RouteQualityReport
from app.schemas.trip_schema import TripPlan, TripRequest
from app.scheduling import ScheduleQualityReport


def make_request() -> TripRequest:
    """创建部分接受策略测试使用的固定两日请求。"""

    return TripRequest(
        city="杭州",
        start_date="2026-08-10",
        end_date="2026-08-11",
        travel_days=2,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["休闲", "自然风光"],
    )


def make_plan(*, day_count: int = 2) -> TripPlan:
    """创建每天至少包含一个景点的结构化行程。"""

    days = []
    for day_index in range(day_count):
        days.append(
            {
                "date": f"2026-08-{10 + day_index:02d}",
                "day_index": day_index,
                "description": f"第 {day_index + 1} 天行程",
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "attractions": [
                    {
                        "name": f"测试景点{day_index + 1}",
                        "address": "杭州市",
                        "location": {
                            "longitude": 120.15 + day_index * 0.01,
                            "latitude": 30.25 + day_index * 0.01,
                        },
                        "visit_duration": 120,
                        "description": "用于测试最低景点保障。",
                    }
                ],
                "meals": [],
            }
        )
    return TripPlan.model_validate(
        {
            "city": "杭州",
            "start_date": "2026-08-10",
            "end_date": "2026-08-11",
            "days": days,
            "weather_info": [],
            "overall_suggestions": "提前预约热门景点。",
        }
    )


def make_validation(code: str, message: str) -> TripValidationResult:
    """创建只包含一个可修复错误的校验结果。"""

    return TripValidationResult.from_issues(
        [
            ValidationIssue(
                code=code,
                severity=ValidationSeverity.ERROR,
                path="overall_suggestions",
                message=message,
                repair_hint="补充对应内容",
                repairable=True,
            )
        ]
    )


def make_state(*, plan: TripPlan | None = None) -> AgentState:
    """创建核心质量报告均达标的 AgentState。"""

    state = AgentState.create(
        make_request(),
        partial_acceptance_enabled=True,
        partial_acceptance_min_score=70,
        partial_acceptance_max_validation_errors=2,
        partial_acceptance_max_schedule_overtime_minutes=60,
        partial_acceptance_max_unavailable_route_legs=0,
        partial_acceptance_max_excessive_commute_segments=0,
        partial_acceptance_max_constraint_errors=0,
        partial_acceptance_min_attractions_per_day=1,
        partial_acceptance_allowed_error_codes=[
            "plan.empty_suggestions",
            "schedule.daily_overtime",
        ],
    )
    state.trip_plan = plan or make_plan()
    state.route_quality_report = RouteQualityReport(plan_fingerprint="route")
    state.schedule_quality_report = ScheduleQualityReport(plan_fingerprint="schedule")
    state.commute_report = CommuteConstraintReport(plan_fingerprint="commute")
    state.constraint_report = TripConstraintReport(plan_fingerprint="constraint")
    return state


class PartialAcceptancePolicyTests(unittest.TestCase):
    def test_accepts_allowlisted_non_critical_error(self):
        """非关键错误在核心质量达标时应带警告完成。"""

        state = make_state()
        validation = make_validation("plan.empty_suggestions", "总体建议不能为空")

        report = PartialAcceptancePolicy.evaluate(state, validation)

        self.assertTrue(report.accepted)
        self.assertTrue(report.partial)
        self.assertEqual(report.quality_level, PlanQualityLevel.ACCEPTABLE)
        self.assertGreaterEqual(report.quality_score, 70)
        self.assertEqual(report.unresolved_issue_codes, ["plan.empty_suggestions"])
        self.assertEqual(report.warnings, ["总体建议不能为空"])
        self.assertFalse(report.blocking_reasons)

    def test_rejects_structural_day_count_error_as_unusable(self):
        """核心天数损坏不能通过部分可接受策略。"""

        state = make_state(plan=make_plan(day_count=1))
        validation = make_validation("days.count_mismatch", "行程天数不一致")

        report = PartialAcceptancePolicy.evaluate(state, validation)

        self.assertFalse(report.accepted)
        self.assertFalse(report.partial)
        self.assertEqual(report.quality_level, PlanQualityLevel.UNUSABLE)
        self.assertFalse(report.core_checks["day_count"])
        self.assertTrue(report.blocking_reasons)

    def test_rejects_unavailable_route_as_degraded(self):
        """结构完整但存在不可用真实路线时只能标记为 degraded。"""

        state = make_state()
        state.route_quality_report = RouteQualityReport(
            plan_fingerprint="route",
            total_legs=1,
            unavailable_legs=1,
            quality_score=70,
        )
        validation = make_validation("plan.empty_suggestions", "总体建议不能为空")

        report = PartialAcceptancePolicy.evaluate(state, validation)

        self.assertFalse(report.accepted)
        self.assertEqual(report.quality_level, PlanQualityLevel.DEGRADED)
        self.assertFalse(report.core_checks["route_availability"])
        self.assertIn("不可用路线分段超过允许上限", report.blocking_reasons)


if __name__ == "__main__":
    unittest.main()
