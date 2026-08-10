"""Deterministic construction and scoring of daily trip timelines."""

from __future__ import annotations

import math
from math import asin, cos, radians, sin, sqrt

from app.providers.amap.models import (
    RouteEstimate,
    RouteEstimateResult,
    RouteLegType,
    RouteMode,
)
from app.routing.plan_routes import (
    normalize_transportation_mode,
    plan_route_fingerprint,
    resolve_day_hotels,
)
from app.schemas.trip_schema import Attraction, DayPlan, Hotel, TripPlan, TripRequest
from app.scheduling.models import DayScheduleQuality, ScheduleQualityReport, TimelineItem


_FALLBACK_SPEED_KMH: dict[RouteMode, float] = {
    "walking": 4.0,
    "driving": 25.0,
    "transit": 18.0,
}


def _parse_clock(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid clock value: {value!r}")
    hour, minute = (int(part) for part in parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Invalid clock value: {value!r}")
    return hour * 60 + minute


def _format_clock(total_minutes: int) -> str:
    """Keep overtime visible instead of wrapping it into the next calendar day."""

    hours, minutes = divmod(max(0, total_minutes), 60)
    return f"{hours:02d}:{minutes:02d}"


def _haversine_km(left: Attraction | Hotel, right: Attraction | Hotel) -> float:
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


def _route_lookup(
    route_estimates: RouteEstimateResult | dict | None,
) -> dict[tuple[int, RouteLegType, int], RouteEstimate]:
    if route_estimates is None:
        return {}
    result = (
        route_estimates
        if isinstance(route_estimates, RouteEstimateResult)
        else RouteEstimateResult.model_validate(route_estimates)
    )
    return {
        (item.day_index, item.leg_type, item.leg_index): item
        for item in result.routes
    }


class ScheduleTimelineEvaluator:
    """Build timelines from plan durations plus real or conservative route times."""

    def __init__(
        self,
        *,
        default_start_time: str = "09:00",
        default_end_time: str = "18:00",
        lunch_duration_minutes: int = 60,
        lunch_window_start: str = "11:30",
        route_buffer_minutes: int = 10,
        attraction_buffer_minutes: int = 10,
        fallback_safety_factor: float = 1.3,
    ):
        self.start_minute = _parse_clock(default_start_time)
        self.end_minute = _parse_clock(default_end_time)
        if self.end_minute <= self.start_minute:
            raise ValueError("default_end_time must be later than default_start_time")
        if min(
            lunch_duration_minutes,
            route_buffer_minutes,
            attraction_buffer_minutes,
        ) < 0:
            raise ValueError("schedule durations cannot be negative")
        if fallback_safety_factor <= 0:
            raise ValueError("fallback_safety_factor must be positive")
        self.lunch_duration_minutes = lunch_duration_minutes
        self.lunch_window_start_minute = _parse_clock(lunch_window_start)
        self.route_buffer_minutes = route_buffer_minutes
        self.attraction_buffer_minutes = attraction_buffer_minutes
        self.fallback_safety_factor = fallback_safety_factor

    def evaluate(
        self,
        request: TripRequest,
        plan: TripPlan,
        route_estimates: RouteEstimateResult | dict | None = None,
    ) -> ScheduleQualityReport:
        """Evaluate every day with stable ordering and no external calls."""

        routes = _route_lookup(route_estimates)
        day_reports = [
            self._evaluate_day(request, plan, day, position, routes)
            for position, day in enumerate(plan.days)
        ]
        feasible_days = sum(1 for day in day_reports if day.feasible)
        overtime = sum(day.overtime_minutes for day in day_reports)
        fallback_legs = sum(day.fallback_route_legs for day in day_reports)
        transportation = sum(day.transportation_minutes for day in day_reports)
        cost = sum(day.optimization_cost for day in day_reports)
        score = (
            round(sum(day.quality_score for day in day_reports) / len(day_reports), 2)
            if day_reports
            else 100.0
        )
        return ScheduleQualityReport(
            plan_fingerprint=plan_route_fingerprint(request, plan),
            feasible_days=feasible_days,
            infeasible_days=len(day_reports) - feasible_days,
            total_overtime_minutes=overtime,
            fallback_route_legs=fallback_legs,
            total_transportation_minutes=transportation,
            optimization_cost=round(cost, 2),
            quality_score=score,
            optimization_recommended=overtime > 0,
            days=day_reports,
        )

    def _evaluate_day(
        self,
        request: TripRequest,
        plan: TripPlan,
        day: DayPlan,
        day_position: int,
        routes: dict[tuple[int, RouteLegType, int], RouteEstimate],
    ) -> DayScheduleQuality:
        day_index = day.day_index if day.day_index >= 0 else day_position
        available_minutes = self.end_minute - self.start_minute
        current = self.start_minute
        timeline: list[TimelineItem] = []
        attraction_minutes = 0
        transportation_minutes = 0
        meal_minutes = 0
        break_minutes = 0
        fallback_legs = 0
        lunch_added = False
        mode = normalize_transportation_mode(day.transportation or request.transportation)
        start_hotel, return_hotel = resolve_day_hotels(plan, day_position)

        def append_item(
            item_type: str,
            name: str,
            duration: int,
            *,
            source_index: int | None = None,
            transportation_time_source: str | None = None,
        ) -> None:
            nonlocal current
            if duration <= 0:
                return
            start = current
            current += duration
            timeline.append(
                TimelineItem(
                    item_type=item_type,
                    name=name,
                    start_time=_format_clock(start),
                    end_time=_format_clock(current),
                    duration_minutes=duration,
                    day_index=day_index,
                    source_index=source_index,
                    transportation_time_source=transportation_time_source,
                )
            )

        lunch_name = self._lunch_name(day)

        # Start each day from its hotel. On checkout days the nearest previous
        # hotel is reused, which keeps the first attraction's travel time visible.
        if day.attractions and start_hotel is not None:
            departure_route = routes.get((day_index, "hotel_departure", 0))
            route_minutes, source = self._transport_minutes(
                start_hotel,
                day.attractions[0],
                mode,
                departure_route,
            )
            if source == "haversine_fallback":
                fallback_legs += 1
            append_item(
                "transportation",
                f"{start_hotel.name} \u2192 {day.attractions[0].name}",
                route_minutes,
                source_index=0,
                transportation_time_source=source,
            )
            transportation_minutes += route_minutes
            if self.route_buffer_minutes:
                append_item(
                    "break",
                    "\u8def\u7ebf\u673a\u52a8\u7f13\u51b2",
                    self.route_buffer_minutes,
                    source_index=0,
                )
                break_minutes += self.route_buffer_minutes

        for attraction_index, attraction in enumerate(day.attractions):
            # A route may cross noon. Insert lunch before the next attraction so
            # the timeline never postpones the meal until that attraction ends.
            if (
                not lunch_added
                and self.lunch_duration_minutes > 0
                and current >= self.lunch_window_start_minute
            ):
                append_item("meal", lunch_name, self.lunch_duration_minutes)
                meal_minutes += self.lunch_duration_minutes
                lunch_added = True

            duration = max(0, int(attraction.visit_duration))
            append_item(
                "attraction",
                attraction.name,
                duration,
                source_index=attraction_index,
            )
            attraction_minutes += duration

            has_next = attraction_index < len(day.attractions) - 1
            if has_next and self.attraction_buffer_minutes:
                append_item(
                    "break",
                    "景点间休息缓冲",
                    self.attraction_buffer_minutes,
                    source_index=attraction_index,
                )
                break_minutes += self.attraction_buffer_minutes

            # Lunch is inserted once the active timeline reaches noon, but only
            # when the day actually spans the lunch period.
            if (
                not lunch_added
                and self.lunch_duration_minutes > 0
                and current >= self.lunch_window_start_minute
                and (has_next or current > 12 * 60)
            ):
                append_item("meal", lunch_name, self.lunch_duration_minutes)
                meal_minutes += self.lunch_duration_minutes
                lunch_added = True

            if not has_next:
                continue
            route = routes.get((day_index, "between_attractions", attraction_index))
            route_minutes, source = self._transport_minutes(
                attraction,
                day.attractions[attraction_index + 1],
                mode,
                route,
            )
            if source == "haversine_fallback":
                fallback_legs += 1
            append_item(
                "transportation",
                f"{attraction.name} → {day.attractions[attraction_index + 1].name}",
                route_minutes,
                source_index=attraction_index,
                transportation_time_source=source,
            )
            transportation_minutes += route_minutes
            if self.route_buffer_minutes:
                append_item(
                    "break",
                    "路线机动缓冲",
                    self.route_buffer_minutes,
                    source_index=attraction_index,
                )
                break_minutes += self.route_buffer_minutes

        # Returning to the current day's hotel is part of the executable
        # schedule. The final checkout day has no return leg when hotel is absent.
        if day.attractions and return_hotel is not None:
            return_route = routes.get((day_index, "hotel_return", 0))
            route_minutes, source = self._transport_minutes(
                day.attractions[-1],
                return_hotel,
                mode,
                return_route,
            )
            if source == "haversine_fallback":
                fallback_legs += 1
            append_item(
                "transportation",
                f"{day.attractions[-1].name} \u2192 {return_hotel.name}",
                route_minutes,
                source_index=max(0, len(day.attractions) - 1),
                transportation_time_source=source,
            )
            transportation_minutes += route_minutes
            if self.route_buffer_minutes:
                append_item(
                    "break",
                    "\u8def\u7ebf\u673a\u52a8\u7f13\u51b2",
                    self.route_buffer_minutes,
                    source_index=max(0, len(day.attractions) - 1),
                )
                break_minutes += self.route_buffer_minutes

        total_required = (
            attraction_minutes + transportation_minutes + meal_minutes + break_minutes
        )
        overtime = max(0, total_required - available_minutes)
        free = max(0, available_minutes - total_required)
        cost = overtime * 100.0 + fallback_legs * 1000.0 + transportation_minutes
        overload_ratio = overtime / available_minutes if available_minutes else 1.0
        transport_ratio = (
            transportation_minutes / available_minutes if available_minutes else 1.0
        )
        score = max(
            0.0,
            min(
                100.0,
                100.0
                - overload_ratio * 100.0
                - fallback_legs * 10.0
                - transport_ratio * 20.0,
            ),
        )
        return DayScheduleQuality(
            day_index=day_index,
            date=day.date,
            available_minutes=available_minutes,
            attraction_minutes=attraction_minutes,
            transportation_minutes=transportation_minutes,
            meal_minutes=meal_minutes,
            break_minutes=break_minutes,
            total_required_minutes=total_required,
            free_minutes=free,
            overtime_minutes=overtime,
            fallback_route_legs=fallback_legs,
            optimization_cost=round(cost, 2),
            quality_score=round(score, 2),
            feasible=overtime == 0,
            timeline=timeline,
        )

    def _transport_minutes(
        self,
        origin: Attraction | Hotel,
        destination: Attraction | Hotel,
        mode: RouteMode,
        route: RouteEstimate | None,
    ) -> tuple[int, str]:
        if route is not None and route.available and route.duration_seconds is not None:
            return max(1, math.ceil(route.duration_seconds / 60.0)), "amap"
        distance = _haversine_km(origin, destination)
        hours = distance / _FALLBACK_SPEED_KMH[mode]
        return (
            max(1, math.ceil(hours * 60.0 * self.fallback_safety_factor)),
            "haversine_fallback",
        )

    @staticmethod
    def _lunch_name(day: DayPlan) -> str:
        for meal in day.meals:
            meal_type = meal.type.strip().lower()
            if "lunch" in meal_type or "午餐" in meal_type:
                return meal.name
        return "午餐"
