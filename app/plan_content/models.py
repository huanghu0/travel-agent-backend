"""最低行程内容回填和确定性内容重建使用的结构化模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.trip_schema import TripPlan


class ContentRefillCandidate(BaseModel):
    """一个用于恢复最低有效景点数量的有界候选行程。"""

    plan: TripPlan
    added_attraction_names: list[str] = Field(default_factory=list)
    added_attraction_ids: list[str] = Field(default_factory=list)
    target_day_indices: list[int] = Field(default_factory=list)
    baseline_attraction_count: int = Field(default=0, ge=0)
    candidate_attraction_count: int = Field(default=0, ge=0)
    considered_candidates: int = Field(default=0, ge=0)
    strategy: str = "refill_nearby_unused_attractions"