"""按交通方式评估单段通勤是否超出可接受时长。"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

from app.commute.models import (
    CommuteConstraintReport,
    CommuteSegmentIssue,
    DayCommuteReport,
)
from app.providers.amap.models import RouteEstimate, RouteEstimateResult, RouteMode
from app.routing import plan_route_fingerprint, resolve_day_hotels
from app.schemas.trip_schema import Attraction, TripPlan, TripRequest


class CommuteConstraintEvaluator:
    """使用不同交通方式的时长阈值，识别真实路线中的过长通勤路段。"""

    def __init__(
        self,
        *,
        max_walking_minutes: int = 45,
        max_transit_minutes: int = 90,
        max_driving_minutes: int = 120,
    ):
        limits = {
            "walking": max_walking_minutes,
            "transit": max_transit_minutes,
            "driving": max_driving_minutes,
        }
        if any(value < 1 for value in limits.values()):
            raise ValueError("Commute duration limits must be at least one minute")
        self._limits_seconds = {
            mode: minutes * 60 for mode, minutes in limits.items()
        }

    def evaluate(
        self,
        request: TripRequest,
        plan: TripPlan,
        routes: RouteEstimateResult | dict,
    ) -> CommuteConstraintReport:
        route_result = (
            routes
            if isinstance(routes, RouteEstimateResult)
            else RouteEstimateResult.model_validate(routes)
        )
        fingerprint = plan_route_fingerprint(request, plan)
        if route_result.plan_fingerprint != fingerprint:
            raise ValueError("Route estimates do not match the current trip plan")

        day_by_index = {
            (day.day_index if day.day_index >= 0 else position): (position, day)
            for position, day in enumerate(plan.days)
        }
        issues: list[CommuteSegmentIssue] = []
        day_routes: dict[int, list[RouteEstimate]] = {}
        for route in route_result.routes:
            day_routes.setdefault(route.day_index, []).append(route)
            if not route.available or route.duration_seconds is None:
                continue
            limit = self._limits_seconds[route.mode]
            if route.duration_seconds <= limit:
                continue
            day_entry = day_by_index.get(route.day_index)
            if day_entry is None or not day_entry[1].attractions:
                continue
            day_position, day = day_entry
            target_index = self._target_attraction_index(
                plan,
                day_position,
                route,
            )
            target = day.attractions[target_index]
            issues.append(
                CommuteSegmentIssue(
                    day_index=route.day_index,
                    leg_index=route.leg_index,
                    leg_type=route.leg_type,
                    origin_name=route.origin_name,
                    destination_name=route.destination_name,
                    mode=route.mode,
                    duration_seconds=route.duration_seconds,
                    distance_meters=int(route.distance_meters or 0),
                    limit_seconds=limit,
                    excess_seconds=route.duration_seconds - limit,
                    target_attraction_name=target.name,
                    target_attraction_index=target_index,
                )
            )

        issues.sort(
            key=lambda item: (
                -item.excess_seconds,
                -item.duration_seconds,
                item.day_index,
                item.leg_index,
                item.leg_type,
            )
        )
        day_reports: list[DayCommuteReport] = []
        for day_index in sorted(day_by_index):
            available = [
                route
                for route in day_routes.get(day_index, [])
                if route.available and route.duration_seconds is not None
            ]
            day_issues = [item for item in issues if item.day_index == day_index]
            day_reports.append(
                DayCommuteReport(
                    day_index=day_index,
                    segment_count=len(available),
                    excessive_segment_count=len(day_issues),
                    max_duration_seconds=max(
                        (int(item.duration_seconds or 0) for item in available),
                        default=0,
                    ),
                    issues=day_issues,
                )
            )

        available_routes = [
            route
            for route in route_result.routes
            if route.available and route.duration_seconds is not None
        ]
        return CommuteConstraintReport(
            plan_fingerprint=fingerprint,
            total_segments=len(available_routes),
            excessive_segment_count=len(issues),
            max_duration_seconds=max(
                (int(route.duration_seconds or 0) for route in available_routes),
                default=0,
            ),
            total_excess_seconds=sum(item.excess_seconds for item in issues),
            optimization_recommended=bool(issues),
            issues=issues,
            days=day_reports,
        )

    @classmethod
    def _target_attraction_index(
        cls,
        plan: TripPlan,
        day_position: int,
        route: RouteEstimate,
    ) -> int:
        day = plan.days[day_position]
        last_index = len(day.attractions) - 1
        if route.leg_type == "hotel_departure":
            return 0
        if route.leg_type == "hotel_return":
            return last_index

        origin_index = min(route.leg_index, last_index)
        destination_index = min(route.leg_index + 1, last_index)
        origin_match = cls._name_matches(
            day.attractions[origin_index].name,
            route.origin_name,
        )
        destination_match = cls._name_matches(
            day.attractions[destination_index].name,
            route.destination_name,
        )
        if origin_match and not destination_match:
            return origin_index
        if destination_match and not origin_match:
            return destination_index

        origin_cost = cls._local_anchor_distance(plan, day_position, origin_index)
        destination_cost = cls._local_anchor_distance(
            plan,
            day_position,
            destination_index,
        )
        return origin_index if origin_cost > destination_cost else destination_index

    @staticmethod
    def _name_matches(left: str, right: str) -> bool:
        normalize = lambda value: "".join(value.lower().split())
        return normalize(left) == normalize(right)

    @classmethod
    def _local_anchor_distance(
        cls,
        plan: TripPlan,
        day_position: int,
        attraction_index: int,
    ) -> float:
        day = plan.days[day_position]
        start_hotel, return_hotel = resolve_day_hotels(plan, day_position)
        current = day.attractions[attraction_index]
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
        return sum(
            cls._haversine_km(current, anchor)
            for anchor in (previous, following)
            if anchor is not None and anchor.location is not None
        )

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
