"""基于高德候选池的确定性最低景点数量保障。"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from pydantic import ValidationError

from app.plan_content.models import ContentRefillCandidate
from app.providers.amap.models import AttractionCandidate
from app.routing.plan_routes import resolve_day_hotels
from app.schemas.trip_schema import Attraction, TripPlan, TripRequest
from app.scheduling import ScheduleTimelineEvaluator


def attraction_identity(*, poi_id: str | None, name: str) -> str:
    """返回供确定性去重使用的稳定景点标识。"""

    normalized_id = "".join(str(poi_id or "").strip().lower().split())
    if normalized_id:
        return f"id:{normalized_id}"
    normalized_name = "".join(name.strip().lower().split())
    return f"name:{normalized_name}"


def count_attractions(plan: TripPlan) -> int:
    return sum(len(day.attractions) for day in plan.days)


class MinimumAttractionRefillOptimizer:
    """使用附近、未使用且时间轴可容纳的 POI 补足行程内容。

    本优化器只生成有界候选；最终是否接受，仍由编排器基于新的真实路线、
    时间轴和可执行性约束进行复验。
    """

    def __init__(
        self,
        *,
        evaluator: ScheduleTimelineEvaluator | None = None,
        minimum_total_attractions: int = 2,
        max_candidates: int = 24,
        default_visit_duration_minutes: int = 120,
    ):
        if minimum_total_attractions < 1:
            raise ValueError("minimum_total_attractions must be at least 1")
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        if default_visit_duration_minutes < 1:
            raise ValueError("default_visit_duration_minutes must be at least 1")
        self.evaluator = evaluator or ScheduleTimelineEvaluator()
        self.minimum_total_attractions = minimum_total_attractions
        self.max_candidates = max_candidates
        self.default_visit_duration_minutes = default_visit_duration_minutes

    def required_total(self, request: TripRequest, plan: TripPlan) -> int:
        """单日行程至少保留一个景点，多日行程默认至少保留两个景点。"""

        day_count = max(1, min(request.travel_days, len(plan.days) or request.travel_days))
        return min(self.minimum_total_attractions, day_count)

    def optimize(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        attractions: dict[str, Any] | None,
        excluded_candidate_identities: set[str] | None = None,
    ) -> ContentRefillCandidate | None:
        baseline_count = count_attractions(plan)
        required = self.required_total(request, plan)
        if baseline_count >= required:
            return None

        excluded = excluded_candidate_identities or set()
        used = {
            attraction_identity(poi_id=item.poi_id, name=item.name)
            for day in plan.days
            for item in day.attractions
        }
        source_candidates = [
            item
            for item in self._source_candidates(attractions)
            if attraction_identity(poi_id=item.poi_id, name=item.name)
            not in used | excluded
        ]
        if not source_candidates:
            return None

        working = plan.model_copy(deep=True)
        available = list(enumerate(source_candidates))
        added_names: list[str] = []
        added_ids: list[str] = []
        target_days: list[int] = []
        considered = 0

        while count_attractions(working) < required and available:
            best: tuple[tuple[Any, ...], TripPlan, int, int, AttractionCandidate] | None = None
            for available_position, (source_order, source) in enumerate(available):
                attraction = self._to_attraction(source)
                preference_score = self._preference_score(request, source)
                rating = source.rating or 0.0
                for day_position, day in enumerate(working.days):
                    for insertion_index in range(len(day.attractions) + 1):
                        if considered >= self.max_candidates:
                            break
                        candidate = working.model_copy(deep=True)
                        candidate.days[day_position].attractions.insert(
                            insertion_index,
                            attraction.model_copy(deep=True),
                        )
                        report = self.evaluator.evaluate(request, candidate, None)
                        considered += 1
                        day_report = report.days[day_position]
                        # 回填不能用新增内容换来一个已知的日程失败；
                        # 真实路线会在后续阶段统一复验。
                        if report.infeasible_days > 0 or report.total_overtime_minutes > 0:
                            continue
                        proximity = self._placement_distance_km(
                            working,
                            day_position,
                            insertion_index,
                            attraction,
                        )
                        stable_day_index = (
                            day.day_index if day.day_index >= 0 else day_position
                        )
                        rank = (
                            report.total_transportation_minutes,
                            day_report.transportation_minutes,
                            round(proximity, 4),
                            -preference_score,
                            -rating,
                            stable_day_index,
                            insertion_index,
                            source_order,
                        )
                        comparison = (
                            rank,
                            candidate,
                            available_position,
                            stable_day_index,
                            source,
                        )
                        if best is None or rank < best[0]:
                            best = comparison
                    if considered >= self.max_candidates:
                        break
                if considered >= self.max_candidates:
                    break

            if best is None:
                return None
            _, working, available_position, stable_day_index, source = best
            added_names.append(source.name)
            added_ids.append(source.poi_id)
            target_days.append(stable_day_index)
            available.pop(available_position)

        candidate_count = count_attractions(working)
        if candidate_count < required:
            return None
        return ContentRefillCandidate(
            plan=working,
            added_attraction_names=added_names,
            added_attraction_ids=added_ids,
            target_day_indices=target_days,
            baseline_attraction_count=baseline_count,
            candidate_attraction_count=candidate_count,
            considered_candidates=considered,
        )

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
            normalized.setdefault("address", "\u5730\u5740\u5f85\u786e\u8ba4")
            try:
                result.append(AttractionCandidate.model_validate(normalized))
            except ValidationError:
                continue
        return result

    def _to_attraction(self, source: AttractionCandidate) -> Attraction:
        details = [value for value in (source.district, source.category) if value]
        if source.rating is not None:
            details.append(f"\u9ad8\u5fb7\u8bc4\u5206 {source.rating:.1f}")
        description = "\uff1b".join(details) or "\u9ad8\u5fb7\u5730\u56fe\u5019\u9009\u666f\u70b9"
        return Attraction(
            name=source.name,
            address=source.address or "\u5730\u5740\u5f85\u786e\u8ba4",
            location=source.location.model_dump(),
            visit_duration=self.default_visit_duration_minutes,
            description=description,
            category=source.category or "\u666f\u70b9",
            rating=source.rating,
            poi_id=source.poi_id,
            ticket_price=0,
        )

    @staticmethod
    def _preference_score(request: TripRequest, source: AttractionCandidate) -> int:
        haystack = "".join(
            (source.name, source.category, source.district, source.address)
        ).lower()
        return sum(
            1 for preference in request.preferences
            if preference.strip() and preference.strip().lower() in haystack
        )

    @classmethod
    def _placement_distance_km(
        cls,
        plan: TripPlan,
        day_position: int,
        insertion_index: int,
        attraction: Attraction,
    ) -> float:
        day = plan.days[day_position]
        start_hotel, return_hotel = resolve_day_hotels(plan, day_position)
        previous = (
            day.attractions[insertion_index - 1]
            if insertion_index > 0
            else start_hotel
        )
        following = (
            day.attractions[insertion_index]
            if insertion_index < len(day.attractions)
            else return_hotel
        )
        distance = 0.0
        if previous is not None and previous.location is not None:
            distance += cls._haversine_km(previous, attraction)
        if following is not None and following.location is not None:
            distance += cls._haversine_km(attraction, following)
        if previous is None and following is None:
            # 没有任何地理锚点的行程无法可靠计算距离，因此确定性地排在最后。
            distance += 10000.0
        return distance

    @staticmethod
    def _haversine_km(left, right) -> float:
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
