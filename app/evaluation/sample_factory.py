"""固定验收录制格式使用的确定性离线样本工厂。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.agent_runtime.acceptance import PartialAcceptanceReport, PlanQualityLevel
from app.agent_runtime.state import AgentState
from app.commute import CommuteConstraintReport
from app.constraints import TripConstraintReport
from app.evaluation.models import AcceptanceScenario
from app.routing import RouteQualityReport
from app.scheduling import ScheduleQualityReport
from app.schemas.trip_schema import Attraction, DayPlan, Location, TripPlan


_FIXTURE_TIME = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)


def build_synthetic_acceptance_state(scenario: AcceptanceScenario) -> AgentState:
    """构造不含真实高德/LLM 数据的通过样本，只验证录制与质量门契约。"""

    request = scenario.request
    state = AgentState.create(
        request,
        session_id=f"fixture-{scenario.case_id}",
        max_steps=24,
    )
    start_date = date.fromisoformat(request.start_date)
    days: list[DayPlan] = []
    for day_index in range(request.travel_days):
        days.append(
            DayPlan(
                date=(start_date + timedelta(days=day_index)).isoformat(),
                day_index=day_index,
                description="固定验收离线契约样本",
                transportation=request.transportation,
                accommodation=request.accommodation,
                attractions=[
                    Attraction(
                        name=f"{request.city}验收景点{day_index + 1}",
                        address="已脱敏固定验收地址",
                        location=Location(
                            longitude=120.0 + day_index * 0.01,
                            latitude=30.0 + day_index * 0.01,
                        ),
                        visit_duration=120,
                        description="用于校验录制格式、回放和质量门，不代表真实地点数据",
                    )
                ],
            )
        )

    fingerprint = f"fixture-{scenario.case_id}"
    state.trip_plan = TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        overall_suggestions="离线契约样本仅用于持续集成，不作为用户行程返回。",
    )
    state.route_quality_report = RouteQualityReport(
        plan_fingerprint=fingerprint,
        quality_score=95.0,
    )
    state.commute_report = CommuteConstraintReport(plan_fingerprint=fingerprint)
    state.schedule_quality_report = ScheduleQualityReport(
        plan_fingerprint=fingerprint,
        feasible_days=request.travel_days,
        quality_score=94.0,
    )
    state.constraint_report = TripConstraintReport(
        plan_fingerprint=fingerprint,
        quality_score=96.0,
    )
    state.acceptance_report = PartialAcceptanceReport(
        accepted=True,
        partial=False,
        quality_level=PlanQualityLevel.EXCELLENT,
        quality_score=95.0,
        evaluated_at=_FIXTURE_TIME,
        reason="固定验收离线契约样本通过",
    )
    state.completion_mode = "full"
    state.status = "completed"
    state.finished = True
    state.current_step = 12
    state.llm_call_count = 1
    state.created_at = _FIXTURE_TIME
    state.updated_at = _FIXTURE_TIME
    state.started_at = _FIXTURE_TIME
    state.deadline_at = _FIXTURE_TIME + timedelta(
        seconds=state.execution_budget.max_duration_seconds
    )
    return state
