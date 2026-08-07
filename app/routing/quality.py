"""Deterministic scoring for real route estimates attached to a trip plan."""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from app.providers.amap.models import RouteEstimate, RouteEstimateResult
from app.schemas.trip_schema import TripPlan


UNAVAILABLE_LEG_PENALTY = 21_600.0
EXCESSIVE_DURATION_SECONDS = 3_600
LONG_DISTANCE_METERS = 30_000
LONG_DAY_DURATION_SECONDS = 7_200
LOW_QUALITY_SCORE = 60.0


class RouteDayQuality(BaseModel):
    """Quality metrics for adjacent attraction legs in one day."""

    day_index: int = Field(ge=0)
    date: str = ""
    attraction_count: int = Field(default=0, ge=0)
    leg_count: int = Field(default=0, ge=0)
    available_legs: int = Field(default=0, ge=0)
    unavailable_legs: int = Field(default=0, ge=0)
    total_distance_meters: int = Field(default=0, ge=0)
    total_duration_seconds: int = Field(default=0, ge=0)
    longest_leg_index: int | None = Field(default=None, ge=0)
    longest_duration_seconds: int | None = Field(default=None, ge=0)
    excessive_duration_legs: int = Field(default=0, ge=0)
    long_distance_legs: int = Field(default=0, ge=0)
    optimization_cost: float = Field(default=0.0, ge=0)
    quality_score: float = Field(default=100.0, ge=0, le=100)
    optimization_recommended: bool = False


class RouteQualityReport(BaseModel):
    """Stable route-quality snapshot tied to one plan fingerprint."""

    plan_fingerprint: str = Field(min_length=1)
    total_legs: int = Field(default=0, ge=0)
    available_legs: int = Field(default=0, ge=0)
    unavailable_legs: int = Field(default=0, ge=0)
    total_distance_meters: int = Field(default=0, ge=0)
    total_duration_seconds: int = Field(default=0, ge=0)
    excessive_duration_legs: int = Field(default=0, ge=0)
    long_distance_legs: int = Field(default=0, ge=0)
    optimization_cost: float = Field(default=0.0, ge=0)
    quality_score: float = Field(default=100.0, ge=0, le=100)
    optimization_recommended: bool = False
    days: list[RouteDayQuality] = Field(default_factory=list)


def _usable_route(route: RouteEstimate | None) -> bool:
    return bool(
        route is not None
        and route.available
        and route.distance_meters is not None
        and route.duration_seconds is not None
    )


def _quality_score(cost: float, leg_count: int) -> float:
    if leg_count <= 0:
        return 100.0
    average_cost = cost / leg_count
    return round(max(0.0, min(100.0, 100.0 * math.exp(-average_cost / 7_200.0))), 2)


def evaluate_route_quality(
    plan: TripPlan,
    route_result: RouteEstimateResult | dict,
) -> RouteQualityReport:
    """Score expected adjacent legs; missing and unavailable legs receive a penalty."""

    result = (
        route_result
        if isinstance(route_result, RouteEstimateResult)
        else RouteEstimateResult.model_validate(route_result)
    )
    indexed_routes = {
        (route.day_index, route.leg_index): route
        for route in result.routes
    }
    day_reports: list[RouteDayQuality] = []

    for day_position, day in enumerate(plan.days):
        day_index = day.day_index if day.day_index >= 0 else day_position
        leg_count = max(0, len(day.attractions) - 1)
        available_legs = 0
        unavailable_legs = 0
        total_distance = 0
        total_duration = 0
        excessive_duration_legs = 0
        long_distance_legs = 0
        longest_leg_index: int | None = None
        longest_duration: int | None = None
        optimization_cost = 0.0

        for leg_index in range(leg_count):
            route = indexed_routes.get((day_index, leg_index))
            if not _usable_route(route):
                unavailable_legs += 1
                optimization_cost += UNAVAILABLE_LEG_PENALTY
                continue

            assert route is not None
            distance = int(route.distance_meters or 0)
            duration = int(route.duration_seconds or 0)
            available_legs += 1
            total_distance += distance
            total_duration += duration
            optimization_cost += duration + distance / 10.0
            if duration > EXCESSIVE_DURATION_SECONDS:
                excessive_duration_legs += 1
            if distance > LONG_DISTANCE_METERS:
                long_distance_legs += 1
            if longest_duration is None or duration > longest_duration:
                longest_duration = duration
                longest_leg_index = leg_index

        score = _quality_score(optimization_cost, leg_count)
        can_reorder = len(day.attractions) >= 3
        recommend = can_reorder and (
            unavailable_legs > 0
            or excessive_duration_legs > 0
            or long_distance_legs > 0
            or total_duration > LONG_DAY_DURATION_SECONDS
            or score < LOW_QUALITY_SCORE
        )
        day_reports.append(
            RouteDayQuality(
                day_index=day_index,
                date=day.date,
                attraction_count=len(day.attractions),
                leg_count=leg_count,
                available_legs=available_legs,
                unavailable_legs=unavailable_legs,
                total_distance_meters=total_distance,
                total_duration_seconds=total_duration,
                longest_leg_index=longest_leg_index,
                longest_duration_seconds=longest_duration,
                excessive_duration_legs=excessive_duration_legs,
                long_distance_legs=long_distance_legs,
                optimization_cost=round(optimization_cost, 2),
                quality_score=score,
                optimization_recommended=recommend,
            )
        )

    total_legs = sum(item.leg_count for item in day_reports)
    optimization_cost = round(sum(item.optimization_cost for item in day_reports), 2)
    return RouteQualityReport(
        plan_fingerprint=result.plan_fingerprint,
        total_legs=total_legs,
        available_legs=sum(item.available_legs for item in day_reports),
        unavailable_legs=sum(item.unavailable_legs for item in day_reports),
        total_distance_meters=sum(item.total_distance_meters for item in day_reports),
        total_duration_seconds=sum(item.total_duration_seconds for item in day_reports),
        excessive_duration_legs=sum(item.excessive_duration_legs for item in day_reports),
        long_distance_legs=sum(item.long_distance_legs for item in day_reports),
        optimization_cost=optimization_cost,
        quality_score=_quality_score(optimization_cost, total_legs),
        optimization_recommended=any(
            item.optimization_recommended for item in day_reports
        ),
        days=day_reports,
    )


def route_quality_improvement_percent(
    before: RouteQualityReport,
    after: RouteQualityReport,
) -> float:
    """Return positive cost reduction as a percentage of the baseline cost."""

    if before.optimization_cost <= 0:
        return 0.0
    improvement = (
        before.optimization_cost - after.optimization_cost
    ) / before.optimization_cost * 100.0
    return round(improvement, 2)


def is_route_quality_improvement(
    before: RouteQualityReport,
    after: RouteQualityReport,
    *,
    min_improvement_percent: float = 10.0,
) -> bool:
    """Accept only material improvements that do not worsen hard route failures."""

    return bool(
        after.optimization_cost < before.optimization_cost
        and route_quality_improvement_percent(before, after)
        >= min_improvement_percent
        and after.unavailable_legs <= before.unavailable_legs
        and after.excessive_duration_legs <= before.excessive_duration_legs
    )
