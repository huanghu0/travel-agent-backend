"""单段通勤约束评估与确定性优化使用的结构化模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.providers.amap.models import GeoPoint, RouteLegType, RouteMode
from app.schemas.trip_schema import TripPlan


class CommuteSegmentIssue(BaseModel):
    """一条真实可用、但耗时超过对应交通方式上限的路线问题。"""

    code: Literal["route.segment_too_long"] = "route.segment_too_long"
    day_index: int = Field(ge=0)
    leg_index: int = Field(ge=0)
    leg_type: RouteLegType
    origin_name: str
    destination_name: str
    mode: RouteMode
    duration_seconds: int = Field(ge=0)
    distance_meters: int = Field(ge=0)
    limit_seconds: int = Field(gt=0)
    excess_seconds: int = Field(gt=0)
    target_attraction_name: str
    target_attraction_index: int = Field(ge=0)


class DayCommuteReport(BaseModel):
    """单个行程日的通勤路段汇总。"""

    day_index: int = Field(ge=0)
    segment_count: int = Field(default=0, ge=0)
    excessive_segment_count: int = Field(default=0, ge=0)
    max_duration_seconds: int = Field(default=0, ge=0)
    issues: list[CommuteSegmentIssue] = Field(default_factory=list)


class CommuteConstraintReport(BaseModel):
    """整个行程中所有真实路线的分交通方式通勤约束报告。"""

    plan_fingerprint: str
    total_segments: int = Field(default=0, ge=0)
    excessive_segment_count: int = Field(default=0, ge=0)
    max_duration_seconds: int = Field(default=0, ge=0)
    total_excess_seconds: int = Field(default=0, ge=0)
    optimization_recommended: bool = False
    issues: list[CommuteSegmentIssue] = Field(default_factory=list)
    days: list[DayCommuteReport] = Field(default_factory=list)


class CommuteReplacementCandidate(BaseModel):
    """保持景点数量不变、用于替换过远景点的候选行程。"""

    plan: TripPlan
    day_index: int = Field(ge=0)
    attraction_index: int = Field(ge=0)
    replaced_attraction_name: str
    replaced_attraction_id: str = ""
    replacement_attraction_name: str
    replacement_attraction_id: str = ""
    source_issue: CommuteSegmentIssue
    approximate_baseline_distance_km: float = Field(default=0.0, ge=0)
    approximate_candidate_distance_km: float = Field(default=0.0, ge=0)
    considered_candidates: int = Field(default=0, ge=0)
    strategy: str = "replace_remote_attraction_with_nearby_unused_candidate"


class CommuteSupplementQuery(BaseModel):
    """针对一个通勤超限问题生成的有界高德周边搜索请求。"""

    city: str = Field(min_length=1)
    keywords: str = Field(min_length=1)
    center: GeoPoint
    radius_meters: int = Field(ge=100, le=50000)
    page: int = Field(default=1, ge=1, le=100)
    page_size: int = Field(default=20, ge=1, le=25)
    day_index: int = Field(ge=0)
    attraction_index: int = Field(ge=0)
    target_attraction_name: str = Field(min_length=1)
    anchor_names: list[str] = Field(default_factory=list)


class CandidatePoolMergeResult(BaseModel):
    """高德周边 POI 合并进候选池后的统计结果。"""

    pool: dict
    received_candidates: int = Field(default=0, ge=0)
    added_candidates: int = Field(default=0, ge=0)
    duplicate_candidates: int = Field(default=0, ge=0)
    final_candidates: int = Field(default=0, ge=0)
