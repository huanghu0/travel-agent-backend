"""对造成通勤超限的景点执行确定性近距离替换。"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from pydantic import ValidationError

from app.commute.models import CommuteConstraintReport, CommuteReplacementCandidate
from app.plan_content import attraction_identity
from app.providers.amap.models import AttractionCandidate
from app.routing import resolve_day_hotels
from app.schemas.trip_schema import Attraction, TripPlan, TripRequest


class RemoteAttractionReplacementOptimizer:
    """用距离锚点更近、尚未使用且满足条件的高德 POI 替换过远景点。

    球面距离只用于对有限候选进行初步排序；候选必须经过新的高德真实路线、
    完整时间轴和可执行性约束复验后，编排器才会正式接受。
    """

    def __init__(
        self,
        *,
        max_candidates: int = 24,
        default_visit_duration_minutes: int = 120,
    ):
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        if default_visit_duration_minutes < 1:
            raise ValueError("default_visit_duration_minutes must be at least 1")
        self.max_candidates = max_candidates
        self.default_visit_duration_minutes = default_visit_duration_minutes

    def optimize(
        self,
        request: TripRequest,
        plan: TripPlan,
        report: CommuteConstraintReport,
        *,
        attractions: dict[str, Any] | None,
        excluded_candidate_identities: set[str] | None = None,
    ) -> CommuteReplacementCandidate | None:
        if not report.issues:
            return None

        excluded = excluded_candidate_identities or set()
        used = {
            attraction_identity(poi_id=item.poi_id, name=item.name)
            for day in plan.days
            for item in day.attractions
        }
        sources = [
            item
            for item in self._source_candidates(attractions)
            if attraction_identity(poi_id=item.poi_id, name=item.name)
            not in used | excluded
        ][: self.max_candidates]
        if not sources:
            return None

        day_positions = {
            (day.day_index if day.day_index >= 0 else position): position
            for position, day in enumerate(plan.days)
        }
        for issue in report.issues:
            day_position = day_positions.get(issue.day_index)
            if day_position is None:
                continue
            day = plan.days[day_position]
            if issue.target_attraction_index >= len(day.attractions):
                continue
            target = day.attractions[issue.target_attraction_index]
            baseline_distance = self._placement_distance_km(
                plan,
                day_position,
                issue.target_attraction_index,
                target,
            )
            ranked: list[tuple[tuple[Any, ...], AttractionCandidate, float]] = []
            for source_order, source in enumerate(sources):
                replacement = self._to_attraction(source, target)
                candidate_distance = self._placement_distance_km(
                    plan,
                    day_position,
                    issue.target_attraction_index,
                    replacement,
                )
                if candidate_distance >= baseline_distance:
                    continue
                rank = (
                    round(candidate_distance, 4),
                    self._category_penalty(target, source),
                    -self._preference_score(request, source),
                    -(source.rating or 0.0),
                    source_order,
                )
                ranked.append((rank, source, candidate_distance))
            if not ranked:
                continue

            _, source, candidate_distance = min(ranked, key=lambda item: item[0])
            replacement = self._to_attraction(source, target)
            candidate_plan = plan.model_copy(deep=True)
            candidate_plan.days[day_position].attractions[
                issue.target_attraction_index
            ] = replacement
            return CommuteReplacementCandidate(
                plan=candidate_plan,
                day_index=issue.day_index,
                attraction_index=issue.target_attraction_index,
                replaced_attraction_name=target.name,
                replaced_attraction_id=target.poi_id or "",
                replacement_attraction_name=source.name,
                replacement_attraction_id=source.poi_id,
                source_issue=issue,
                approximate_baseline_distance_km=round(baseline_distance, 4),
                approximate_candidate_distance_km=round(candidate_distance, 4),
                considered_candidates=len(sources),
            )
        return None

    @staticmethod
    def _source_candidates(payload: dict[str, Any] | None) -> list[AttractionCandidate]:
        if not isinstance(payload, dict):
            return []
        raw = payload.get("candidates")
        if not isinstance(raw, list):
            raw = payload.get("pois")
        if not isinstance(raw, list):
            return []
        result: list[AttractionCandidate] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized.setdefault("poi_id", normalized.get("id", ""))
            normalized.setdefault("address", "地址待确认")
            try:
                result.append(AttractionCandidate.model_validate(normalized))
            except ValidationError:
                continue
        return result

    def _to_attraction(
        self,
        source: AttractionCandidate,
        target: Attraction,
    ) -> Attraction:
        details = [value for value in (source.district, source.category) if value]
        if source.rating is not None:
            details.append(f"高德评分 {source.rating:.1f}")
        return Attraction(
            name=source.name,
            address=source.address or "地址待确认",
            location=source.location.model_dump(),
            visit_duration=target.visit_duration or self.default_visit_duration_minutes,
            description="；".join(details) or "高德地图近距离替换候选景点",
            category=source.category or target.category or "景点",
            rating=source.rating,
            poi_id=source.poi_id,
            ticket_price=0,
        )

    @staticmethod
    def _category_penalty(
        target: Attraction,
        source: AttractionCandidate,
    ) -> int:
        target_category = "".join((target.category or "").lower().split())
        source_category = "".join((source.category or "").lower().split())
        if not target_category or not source_category:
            return 1
        return 0 if target_category in source_category or source_category in target_category else 1

    @staticmethod
    def _preference_score(request: TripRequest, source: AttractionCandidate) -> int:
        haystack = "".join(
            (source.name, source.category, source.district, source.address)
        ).lower()
        return sum(
            1
            for preference in request.preferences
            if preference.strip() and preference.strip().lower() in haystack
        )

    @classmethod
    def _placement_distance_km(
        cls,
        plan: TripPlan,
        day_position: int,
        attraction_index: int,
        attraction: Attraction,
    ) -> float:
        day = plan.days[day_position]
        start_hotel, return_hotel = resolve_day_hotels(plan, day_position)
        previous = (
            day.attractions[attraction_index - 1]
            if attraction_index > 0
            else start_hotel
        )
        following = (
            day.attractions[attraction_index + 1]
            if attraction_index + 1 < len(day.attractions)
            else return_hotel
        )
        anchors = [
            anchor
            for anchor in (previous, following)
            if anchor is not None and anchor.location is not None
        ]
        if not anchors:
            return 10000.0
        return sum(cls._haversine_km(attraction, anchor) for anchor in anchors)

    @staticmethod
    def _haversine_km(left: Attraction, right) -> float:
        radius = 6371.0
        d_lat = radians(right.location.latitude - left.location.latitude)
        d_lon = radians(right.location.longitude - left.location.longitude)
        value = (
            sin(d_lat / 2.0) ** 2
            + cos(radians(left.location.latitude))
            * cos(radians(right.location.latitude))
            * sin(d_lon / 2.0) ** 2
        )
        return 2.0 * radius * asin(sqrt(value))
