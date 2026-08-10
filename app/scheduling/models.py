"""确定性行程时间轴评估与优化使用的结构化输出。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.trip_schema import TripPlan


TimelineItemType = Literal["attraction", "transportation", "meal", "break"]
TransportationTimeSource = Literal["amap", "haversine_fallback"]


class TimelineItem(BaseModel):
    """确定性单日时间轴上的一个连续活动项。"""

    item_type: TimelineItemType
    name: str
    start_time: str
    end_time: str
    duration_minutes: int = Field(ge=0)
    day_index: int = Field(ge=0)
    source_index: int | None = Field(default=None, ge=0)
    transportation_time_source: TransportationTimeSource | None = None


class DayScheduleQuality(BaseModel):
    """单个旅行日的时间轴容量与质量指标。"""

    day_index: int = Field(ge=0)
    date: str = ""
    available_minutes: int = Field(default=0, ge=0)
    attraction_minutes: int = Field(default=0, ge=0)
    transportation_minutes: int = Field(default=0, ge=0)
    meal_minutes: int = Field(default=0, ge=0)
    break_minutes: int = Field(default=0, ge=0)
    total_required_minutes: int = Field(default=0, ge=0)
    free_minutes: int = Field(default=0, ge=0)
    overtime_minutes: int = Field(default=0, ge=0)
    fallback_route_legs: int = Field(default=0, ge=0)
    optimization_cost: float = Field(default=0.0, ge=0)
    quality_score: float = Field(default=100.0, ge=0, le=100)
    feasible: bool = True
    timeline: list[TimelineItem] = Field(default_factory=list)


class ScheduleQualityReport(BaseModel):
    """与指定行程指纹绑定的稳定日程质量快照。"""

    plan_fingerprint: str = Field(min_length=1)
    feasible_days: int = Field(default=0, ge=0)
    infeasible_days: int = Field(default=0, ge=0)
    total_overtime_minutes: int = Field(default=0, ge=0)
    fallback_route_legs: int = Field(default=0, ge=0)
    total_transportation_minutes: int = Field(default=0, ge=0)
    optimization_cost: float = Field(default=0.0, ge=0)
    quality_score: float = Field(default=100.0, ge=0, le=100)
    optimization_recommended: bool = False
    days: list[DayScheduleQuality] = Field(default_factory=list)


class ScheduleOptimizationCandidate(BaseModel):
    """一个等待真实路线复验的有界跨日调整候选。"""

    plan: TripPlan
    source_day_index: int = Field(ge=0)
    target_day_index: int | None = Field(default=None, ge=0)
    moved_attraction_name: str
    target_insertion_index: int | None = Field(default=None, ge=0)
    removed_attraction_names: list[str] = Field(default_factory=list)
    strategy: str
    baseline_cost: float = Field(ge=0)
    candidate_cost: float = Field(ge=0)
    approximate_improvement_percent: float
    considered_candidates: int = Field(default=0, ge=0)
