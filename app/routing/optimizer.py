"""使用地理距离进行有界、确定性的景点顺序优化。"""

from __future__ import annotations

import math
from collections.abc import Iterable

from pydantic import BaseModel, Field

from app.schemas.trip_schema import Attraction, TripPlan


class RouteOptimizationCandidate(BaseModel):
    """当前最优的有界候选，后续仍必须通过真实路线结果验证。"""

    plan: TripPlan
    changed_day_index: int = Field(ge=0)
    strategy: str
    original_distance_meters: float = Field(ge=0)
    candidate_distance_meters: float = Field(ge=0)
    approximate_improvement_percent: float
    considered_candidates: int = Field(default=0, ge=0)


def _haversine_meters(left: Attraction, right: Attraction) -> float:
    radius = 6_371_000.0
    lat1 = math.radians(left.location.latitude)
    lat2 = math.radians(right.location.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(right.location.longitude - left.location.longitude)
    value = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return radius * 2.0 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1.0 - value)))


def _path_distance(attractions: list[Attraction]) -> float:
    return sum(
        _haversine_meters(attractions[index], attractions[index + 1])
        for index in range(max(0, len(attractions) - 1))
    )


def _identity(attraction: Attraction) -> tuple[str, str, float, float]:
    return (
        attraction.poi_id or "",
        attraction.name,
        round(attraction.location.longitude, 7),
        round(attraction.location.latitude, 7),
    )


def _order_key(attractions: list[Attraction]) -> tuple[tuple[str, str, float, float], ...]:
    return tuple(_identity(item) for item in attractions)


def _nearest_neighbor(attractions: list[Attraction]) -> list[Attraction]:
    ordered = [attractions[0]]
    remaining = list(enumerate(attractions[1:], start=1))
    while remaining:
        current = ordered[-1]
        _, selected_index, selected = min(
            (
                (_haversine_meters(current, item), original_index, item)
                for original_index, item in remaining
            ),
            key=lambda value: (value[0], value[1], _identity(value[2])),
        )
        ordered.append(selected)
        remaining = [
            pair for pair in remaining if pair[0] != selected_index
        ]
    return ordered


def _bounded_orders(attractions: list[Attraction]) -> Iterable[tuple[str, list[Attraction]]]:
    """固定首个景点，并按稳定顺序生成确定性的重排策略。"""

    yield "nearest_neighbor", _nearest_neighbor(attractions)
    count = len(attractions)
    for start in range(1, count - 1):
        for end in range(start + 1, count):
            candidate = list(attractions)
            candidate[start : end + 1] = reversed(candidate[start : end + 1])
            yield f"two_opt_{start}_{end}", candidate
    for source in range(1, count):
        for target in range(1, count):
            if source == target:
                continue
            candidate = list(attractions)
            item = candidate.pop(source)
            candidate.insert(target, item)
            yield f"relocate_{source}_{target}", candidate


class DeterministicRouteOptimizer:
    """不调用外部服务、不使用随机性，只选择一个近似成本更低的候选。"""

    def __init__(self, max_candidates: int = 6):
        if max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        self.max_candidates = max_candidates

    def optimize(self, plan: TripPlan) -> RouteOptimizationCandidate | None:
        best: tuple[float, int, str, list[Attraction], float, int] | None = None
        considered = 0
        seen: set[tuple[int, tuple[tuple[str, str, float, float], ...]]] = set()

        for day_position, day in enumerate(plan.days):
            attractions = list(day.attractions)
            if len(attractions) < 3:
                continue
            original_key = _order_key(attractions)
            original_distance = _path_distance(attractions)
            for strategy, order in _bounded_orders(attractions):
                key = (day_position, _order_key(order))
                if key in seen or key[1] == original_key:
                    continue
                seen.add(key)
                considered += 1
                candidate_distance = _path_distance(order)
                if candidate_distance + 0.01 < original_distance:
                    comparison = (
                        candidate_distance,
                        day_position,
                        strategy,
                        order,
                        original_distance,
                        considered,
                    )
                    if best is None or comparison[:3] < best[:3]:
                        best = comparison
                if considered >= self.max_candidates:
                    break
            if considered >= self.max_candidates:
                break

        if best is None:
            return None

        candidate_distance, day_position, strategy, order, original_distance, _ = best
        candidate_plan = plan.model_copy(deep=True)
        candidate_plan.days[day_position].attractions = [
            item.model_copy(deep=True) for item in order
        ]
        improvement = (
            (original_distance - candidate_distance) / original_distance * 100.0
            if original_distance > 0
            else 0.0
        )
        stable_day_index = candidate_plan.days[day_position].day_index
        if stable_day_index < 0:
            stable_day_index = day_position
        return RouteOptimizationCandidate(
            plan=candidate_plan,
            changed_day_index=stable_day_index,
            strategy=strategy,
            original_distance_meters=round(original_distance, 2),
            candidate_distance_meters=round(candidate_distance, 2),
            approximate_improvement_percent=round(improvement, 2),
            considered_candidates=considered,
        )
