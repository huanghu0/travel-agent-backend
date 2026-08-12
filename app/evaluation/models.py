"""固定端到端验收基线使用的请求、阈值和报告模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.trip_schema import TripRequest


class AcceptanceThresholds(BaseModel):
    """所有固定验收场景共享的最低可交付阈值。"""

    min_quality_score: float = Field(default=70.0, ge=0.0, le=100.0)
    min_attractions_per_day: int = Field(default=1, ge=0)
    max_unavailable_route_legs: int = Field(default=0, ge=0)
    max_excessive_commute_segments: int = Field(default=0, ge=0)
    max_schedule_overtime_minutes: int = Field(default=60, ge=0)
    max_constraint_errors: int = Field(default=0, ge=0)
    max_physical_steps: int = Field(default=24, ge=1)
    max_llm_calls: int = Field(default=6, ge=0)
    allowed_completion_modes: list[Literal["full", "partial"]] = Field(
        default_factory=lambda: ["full", "partial"]
    )


class AcceptanceScenario(BaseModel):
    """一个固定且可重复执行的端到端旅行规划输入。"""

    case_id: str
    description: str
    request: TripRequest
    tags: list[str] = Field(default_factory=list)
    thresholds: AcceptanceThresholds = Field(default_factory=AcceptanceThresholds)


class AcceptanceCheckResult(BaseModel):
    """单个可执行性或质量门槛的判断结果。"""

    code: str
    passed: bool
    message: str
    expected: object | None = None
    actual: object | None = None


class AcceptanceCaseResult(BaseModel):
    """固定场景与最近一次匹配会话的验收结果。"""

    case_id: str
    city: str
    travel_days: int = Field(ge=1)
    transportation: str
    status: Literal["missing", "passed", "failed"]
    session_id: str | None = None
    completion_mode: Literal["full", "partial"] | None = None
    quality_level: str | None = None
    quality_score: float | None = Field(default=None, ge=0.0, le=100.0)
    checks: list[AcceptanceCheckResult] = Field(default_factory=list)
    failed_check_codes: list[str] = Field(default_factory=list)


class FixedAcceptanceBaselineReport(BaseModel):
    """固定场景覆盖率和通过率报告。"""

    baseline_version: int = 1
    suite_name: str = "travel-agent-fixed-e2e-v1"
    generated_at: datetime
    requested_limit: int = Field(ge=1)
    sampled_session_count: int = Field(ge=0)
    invalid_session_count: int = Field(ge=0)
    total_case_count: int = Field(ge=0)
    covered_case_count: int = Field(ge=0)
    passed_case_count: int = Field(ge=0)
    failed_case_count: int = Field(ge=0)
    missing_case_count: int = Field(ge=0)
    coverage_rate: float = Field(ge=0.0, le=1.0)
    overall_pass_rate: float = Field(ge=0.0, le=1.0)
    evaluated_pass_rate: float = Field(ge=0.0, le=1.0)
    cases: list[AcceptanceCaseResult] = Field(default_factory=list)
