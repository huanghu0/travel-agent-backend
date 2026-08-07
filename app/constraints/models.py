"""Structured results for deterministic trip execution-constraint evaluation."""

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
    """One deterministic feasibility issue tied to a plan location or day."""

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
    """Constraint summary for one travel day."""

    day_index: int = Field(ge=0)
    date: str = ""
    error_count: int = Field(default=0, ge=0)
    warning_count: int = Field(default=0, ge=0)
    optimization_cost: float = Field(default=0.0, ge=0)
    feasible: bool = True
    issues: list[ConstraintIssue] = Field(default_factory=list)


class TripConstraintReport(BaseModel):
    """Stable feasibility report for one complete trip plan."""

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
    """One bounded plan mutation awaiting real-route verification."""

    plan: TripPlan
    source_day_index: int = Field(ge=0)
    target_day_index: int = Field(ge=0)
    moved_attraction_name: str
    target_insertion_index: int = Field(ge=0)
    strategy: str
    baseline_cost: float = Field(ge=0)
    candidate_cost: float = Field(ge=0)
    approximate_improvement_percent: float
    considered_candidates: int = Field(default=0, ge=0)
