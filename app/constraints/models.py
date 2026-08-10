"""确定性行程可执行性约束评估使用的结构化结果。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.trip_schema import TripPlan


ConstraintSeverity = Literal["error", "warning"]
ConstraintOptimizationStatus = Literal[
    "not_started",
    "candidate_pending",
    "completed",
    "skipped",
]


class ConstraintIssue(BaseModel):
    """一个与指定地点或行程日绑定的确定性可执行性问题。"""

    code: str
    severity: ConstraintSeverity
    path: str
    message: str
    repair_hint: str
    repairable: bool = True
    day_index: int = Field(ge=0)
    source_index: int | None = Field(default=None, ge=0)
    attraction_name: str | None = None
    penalty: float = Field(default=0.0, ge=0)
    expected: object | None = None
    actual: object | None = None


class DayConstraintReport(BaseModel):
    """单个旅行日的可执行性约束汇总。"""

    day_index: int = Field(ge=0)
    date: str = ""
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    optimization_cost: float = Field(default=0.0, ge=0)
    feasible: bool = True
    issues: list[ConstraintIssue] = Field(default_factory=list)


class TripConstraintReport(BaseModel):
    """完整行程对应的稳定可执行性报告。"""

    plan_fingerprint: str = Field(min_length=1)
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    repairable_issue_count: int = Field(default=0, ge=0)
    optimization_cost: float = Field(default=0.0, ge=0)
    quality_score: float = Field(default=100.0, ge=0, le=100)
    feasible: bool = True
    optimization_recommended: bool = False
    days: list[DayConstraintReport] = Field(default_factory=list)
    issues: list[ConstraintIssue] = Field(default_factory=list)


class ConstraintOptimizationCandidate(BaseModel):
    """一个等待真实路线复验的有界行程修改候选。"""

    plan: TripPlan
    source_day_index: int = Field(ge=0)
    target_day_index: int = Field(ge=0)
    moved_attraction_name: str
    target_insertion_index: int = Field(ge=0)
    removed_attraction_names: list[str] = Field(default_factory=list)
    strategy: str
    baseline_cost: float = Field(ge=0)
    candidate_cost: float = Field(ge=0)
    approximate_improvement_percent: float
    considered_candidates: int = Field(default=0, ge=0)
