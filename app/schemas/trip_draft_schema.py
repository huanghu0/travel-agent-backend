"""行程编辑草稿、重新评估结果和版本快照模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from app.agent_runtime.acceptance import PartialAcceptanceReport
from app.commute import CommuteConstraintReport
from app.constraints import TripConstraintReport
from app.providers.amap.models import RouteEstimateResult
from app.routing import RouteQualityReport
from app.schemas.trip_schema import TripPlan
from app.scheduling import ScheduleQualityReport
from app.validation import TripValidationResult


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TripPlanDiff(BaseModel):
    """草稿相对基线版本的结构化差异和增量路线统计。"""

    changed_fields: list[str] = Field(default_factory=list)
    changed_days: list[int] = Field(default_factory=list)
    changed_attractions: list[str] = Field(default_factory=list)
    changed_hotels: list[int] = Field(default_factory=list)
    changed_meals: list[int] = Field(default_factory=list)
    affected_route_keys: list[str] = Field(default_factory=list)
    reused_route_legs: int = Field(default=0, ge=0)
    queried_route_legs: int = Field(default=0, ge=0)


class VersionQualitySnapshot(BaseModel):
    """供前端直接比较的轻量质量快照。"""

    version_number: int = Field(ge=1)
    quality_score: float | None = Field(default=None, ge=0, le=100)
    quality_level: str | None = None
    accepted: bool = False
    route_score: float | None = Field(default=None, ge=0, le=100)
    schedule_score: float | None = Field(default=None, ge=0, le=100)
    unavailable_route_legs: int = Field(default=0, ge=0)
    schedule_overtime_minutes: int = Field(default=0, ge=0)
    excessive_commute_segments: int = Field(default=0, ge=0)
    constraint_errors: int = Field(default=0, ge=0)
    validation_errors: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)


class TripVersionEvaluation(BaseModel):
    """一个版本对应的完整确定性评估产物。"""

    route_estimates: RouteEstimateResult
    route_quality_report: RouteQualityReport
    schedule_quality_report: ScheduleQualityReport
    commute_report: CommuteConstraintReport
    constraint_report: TripConstraintReport
    validation_result: TripValidationResult
    acceptance_report: PartialAcceptanceReport


class TripPlanVersion(BaseModel):
    """可审计、可确认、不可原地覆盖的行程版本。"""

    version_id: str
    session_id: str
    version_number: int = Field(ge=1)
    status: Literal["candidate", "confirmed", "superseded"]
    source: Literal["original", "draft"]
    source_draft_id: str | None = None
    trip_plan: TripPlan
    evaluation: TripVersionEvaluation
    created_at: datetime = Field(default_factory=utc_now)
    confirmed_at: datetime | None = None

    def quality_snapshot(self) -> VersionQualitySnapshot:
        acceptance = self.evaluation.acceptance_report
        validation = self.evaluation.validation_result
        return VersionQualitySnapshot(
            version_number=self.version_number,
            quality_score=acceptance.quality_score,
            quality_level=acceptance.quality_level.value,
            accepted=acceptance.accepted,
            route_score=self.evaluation.route_quality_report.quality_score,
            schedule_score=self.evaluation.schedule_quality_report.quality_score,
            unavailable_route_legs=self.evaluation.route_quality_report.unavailable_legs,
            schedule_overtime_minutes=self.evaluation.schedule_quality_report.total_overtime_minutes,
            excessive_commute_segments=self.evaluation.commute_report.excessive_segment_count,
            constraint_errors=self.evaluation.constraint_report.error_count,
            validation_errors=validation.error_count,
            warnings=acceptance.warnings,
            blocking_reasons=acceptance.blocking_reasons,
        )


class TripDraftCreate(BaseModel):
    """创建草稿；base_version 为空时使用当前确认版本。"""

    base_version: int | None = Field(default=None, ge=1)
    trip_plan: TripPlan


class TripDraftUpdate(BaseModel):
    trip_plan: TripPlan


class TripDraft(BaseModel):
    """用户可反复修改的行程草稿。"""

    draft_id: str
    session_id: str
    base_version: int = Field(ge=1)
    status: Literal["editing", "evaluated", "confirmed", "superseded"] = "editing"
    trip_plan: TripPlan
    diff: TripPlanDiff | None = None
    candidate_version_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DraftEvaluationResponse(BaseModel):
    """草稿重评接口返回的前后对比数据。"""

    draft: TripDraft
    candidate_version: TripPlanVersion
    before: VersionQualitySnapshot
    after: VersionQualitySnapshot
    diff: TripPlanDiff


class ConfirmDraftResponse(BaseModel):
    draft: TripDraft
    confirmed_version: TripPlanVersion
