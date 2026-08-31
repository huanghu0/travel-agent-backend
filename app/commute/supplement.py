"""规划有界高德周边搜索，并将返回的 POI 合并到景点候选池。"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.commute.models import (
    CandidatePoolMergeResult,
    CommuteConstraintReport,
    CommuteSupplementQuery,
)
from app.plan_content import attraction_identity
from app.providers.amap.models import AttractionCandidate, AttractionSearchResult, GeoPoint
from app.routing import resolve_day_hotels
from app.schemas.trip_schema import TripPlan, TripRequest


class CommuteCandidatePoolSupplementer:
    """构造确定性的高德周边搜索请求，并合并标准化候选。

    搜索中心由目标景点前后的相邻地点计算，不把过远目标本身作为锚点；
    后续尝试按倍数扩大半径，同时始终遵守高德周边搜索 50 公里上限。
    """

    def __init__(
        self,
        *,
        initial_radius_meters: int = 5000,
        max_radius_meters: int = 20000,
        page_size: int = 20,
        pool_max_candidates: int = 48,
    ):
        if not 100 <= initial_radius_meters <= 50000:
            raise ValueError("initial_radius_meters must be between 100 and 50000")
        if not initial_radius_meters <= max_radius_meters <= 50000:
            raise ValueError(
                "max_radius_meters must be between initial_radius_meters and 50000"
            )
        if not 1 <= page_size <= 25:
            raise ValueError("page_size must be between 1 and 25")
        if pool_max_candidates < 1:
            raise ValueError("pool_max_candidates must be at least 1")
        self.initial_radius_meters = initial_radius_meters
        self.max_radius_meters = max_radius_meters
        self.page_size = page_size
        self.pool_max_candidates = pool_max_candidates

    def build_query(
        self,
        request: TripRequest,
        plan: TripPlan,
        report: CommuteConstraintReport,
        *,
        search_index: int,
    ) -> CommuteSupplementQuery | None:
        """为第一个可处理的通勤问题构造可重复执行的周边搜索请求。"""

        if search_index < 0:
            raise ValueError("search_index cannot be negative")
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
            start_hotel, return_hotel = resolve_day_hotels(plan, day_position)
            previous = (
                day.attractions[issue.target_attraction_index - 1]
                if issue.target_attraction_index > 0
                else start_hotel
            )
            following = (
                day.attractions[issue.target_attraction_index + 1]
                if issue.target_attraction_index + 1 < len(day.attractions)
                else return_hotel
            )
            anchors = [
                place
                for place in (previous, following)
                if place is not None and place.location is not None
            ]
            if not anchors:
                continue
            center = GeoPoint(
                longitude=sum(item.location.longitude for item in anchors) / len(anchors),
                latitude=sum(item.location.latitude for item in anchors) / len(anchors),
            )
            radius = min(
                self.max_radius_meters,
                self.initial_radius_meters * (2**search_index),
            )
            keywords = ",".join(
                item.strip() for item in request.preferences if item and item.strip()
            ) or "\u666f\u70b9"
            return CommuteSupplementQuery(
                city=request.city,
                keywords=keywords,
                center=center,
                radius_meters=radius,
                page=1,
                page_size=self.page_size,
                day_index=issue.day_index,
                attraction_index=issue.target_attraction_index,
                target_attraction_name=issue.target_attraction_name,
                anchor_names=[item.name for item in anchors],
            )
        return None

    def build_content_refill_query(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        search_index: int,
    ) -> CommuteSupplementQuery | None:
        """围绕景点较少日期的已有景点或酒店构造最低内容补搜请求。"""

        if search_index < 0:
            raise ValueError("search_index cannot be negative")

        ordered_days = sorted(
            enumerate(plan.days),
            key=lambda item: (len(item[1].attractions), item[0]),
        )
        for day_position, day in ordered_days:
            start_hotel, return_hotel = resolve_day_hotels(plan, day_position)
            places = [*day.attractions, start_hotel, return_hotel]
            anchors = []
            seen: set[tuple[str, float, float]] = set()
            for place in places:
                if place is None or place.location is None:
                    continue
                identity = (
                    "".join(place.name.strip().lower().split()),
                    place.location.longitude,
                    place.location.latitude,
                )
                if identity in seen:
                    continue
                seen.add(identity)
                anchors.append(place)
            if not anchors:
                continue

            center = GeoPoint(
                longitude=sum(item.location.longitude for item in anchors) / len(anchors),
                latitude=sum(item.location.latitude for item in anchors) / len(anchors),
            )
            radius = min(
                self.max_radius_meters,
                self.initial_radius_meters * (2**search_index),
            )
            keywords = ",".join(
                item.strip() for item in request.preferences if item and item.strip()
            ) or "景点"
            return CommuteSupplementQuery(
                city=request.city,
                keywords=keywords,
                center=center,
                radius_meters=radius,
                page=1,
                page_size=self.page_size,
                day_index=day.day_index if day.day_index >= 0 else day_position,
                attraction_index=len(day.attractions),
                target_attraction_name="最低景点数量回填",
                anchor_names=[item.name for item in anchors],
            )
        return None

    def merge(
        self,
        existing: dict[str, Any] | None,
        incoming: AttractionSearchResult,
    ) -> CandidatePoolMergeResult:
        """在不修改原持久化对象的前提下，追加合法且未重复的 POI。"""

        existing_payload = dict(existing) if isinstance(existing, dict) else {}
        current = self._candidates(existing_payload)
        received = list(incoming.candidates)
        merged: list[AttractionCandidate] = []
        positions: dict[str, int] = {}
        duplicate_count = 0

        for candidate in [*current, *received]:
            identity = attraction_identity(
                poi_id=candidate.poi_id,
                name=candidate.name,
            )
            if identity in positions:
                duplicate_count += 1
                position = positions[identity]
                old = merged[position]
                if (candidate.rating or -1.0) > (old.rating or -1.0):
                    merged[position] = candidate
                continue
            positions[identity] = len(merged)
            merged.append(candidate)

        cropped = merged[: self.pool_max_candidates]
        current_identities = {
            attraction_identity(poi_id=item.poi_id, name=item.name) for item in current
        }
        added = sum(
            1
            for item in cropped
            if attraction_identity(poi_id=item.poi_id, name=item.name)
            not in current_identities
        )
        output = {
            "provider": existing_payload.get("provider", incoming.provider),
            "query_city": existing_payload.get("query_city", incoming.query_city),
            "keywords": existing_payload.get("keywords", incoming.keywords),
            "total_received": max(
                int(existing_payload.get("total_received", 0) or 0),
                len(cropped),
            ),
            "candidates": [item.model_dump(mode="json") for item in cropped],
        }
        return CandidatePoolMergeResult(
            pool=output,
            received_candidates=len(received),
            added_candidates=added,
            duplicate_candidates=duplicate_count,
            final_candidates=len(cropped),
        )

    @staticmethod
    def _candidates(payload: dict[str, Any]) -> list[AttractionCandidate]:
        raw = payload.get("candidates")
        if not isinstance(raw, list):
            raw = payload.get("pois")
        if not isinstance(raw, list):
            return []
        candidates: list[AttractionCandidate] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized.setdefault("poi_id", normalized.get("id", ""))
            normalized.setdefault("address", "Address pending confirmation")
            try:
                candidates.append(AttractionCandidate.model_validate(normalized))
            except ValidationError:
                continue
        return candidates
