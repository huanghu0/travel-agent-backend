"""行程结构发生变化后，确定性重建描述、用餐安排和预算。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any

from app.providers.amap.models import RouteEstimate, RouteEstimateResult
from app.routing.plan_routes import normalize_transportation_mode, resolve_day_hotels
from app.schemas.trip_schema import Budget, Hotel, Meal, TripPlan, TripRequest
from app.scheduling import ScheduleQualityReport


_MEAL_COSTS = {"breakfast": 25, "lunch": 50, "dinner": 70}
_MODE_LABELS = {"walking": "\u6b65\u884c", "driving": "\u9a7e\u8f66", "transit": "\u516c\u5171\u4ea4\u901a"}
_LEG_ORDER = {"hotel_departure": 0, "between_attractions": 1, "hotel_return": 2}


def plan_content_source_fingerprint(
    request: TripRequest,
    plan: TripPlan,
    route_estimates: RouteEstimateResult | dict[str, Any] | None,
    schedule_quality_report: ScheduleQualityReport | dict[str, Any] | None = None,
) -> str:
    """只对决定重建描述和总费用的源字段生成指纹。

    自动生成的描述、餐饮和预算不参与指纹，避免内容重建本身让触发指纹失效。
    """

    routes = _route_result(route_estimates)
    schedule = _schedule_report(schedule_quality_report)
    payload = {
        "request": {
            "city": request.city,
            "transportation_mode": normalize_transportation_mode(request.transportation),
            "accommodation": request.accommodation,
            "preferences": list(request.preferences),
        },
        "days": [
            {
                "date": day.date,
                "day_index": day.day_index,
                "hotel": _hotel_source(day.hotel),
                "attractions": [
                    {
                        "name": item.name,
                        "address": item.address,
                        "poi_id": item.poi_id or "",
                        "visit_duration": item.visit_duration,
                        "ticket_price": item.ticket_price,
                        "longitude": round(item.location.longitude, 6),
                        "latitude": round(item.location.latitude, 6),
                    }
                    for item in day.attractions
                ],
            }
            for day in plan.days
        ],
        "routes": [
            {
                "day_index": item.day_index,
                "leg_index": item.leg_index,
                "leg_type": item.leg_type,
                "origin_name": item.origin_name,
                "destination_name": item.destination_name,
                "mode": item.mode,
                "available": item.available,
                "distance_meters": item.distance_meters,
                "duration_seconds": item.duration_seconds,
            }
            for item in (routes.routes if routes is not None else [])
        ],
        "schedule": (
            {
                "infeasible_days": schedule.infeasible_days,
                "total_overtime_minutes": schedule.total_overtime_minutes,
                "total_transportation_minutes": schedule.total_transportation_minutes,
            }
            if schedule is not None
            else None
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TripPlanConsistencyRebuilder:
    """根据最终酒店、景点和真实路线重建全部派生内容。"""

    def rebuild(
        self,
        request: TripRequest,
        plan: TripPlan,
        *,
        route_estimates: RouteEstimateResult | dict[str, Any] | None,
        schedule_quality_report: ScheduleQualityReport | dict[str, Any] | None = None,
    ) -> TripPlan:
        rebuilt = plan.model_copy(deep=True)
        route_result = _route_result(route_estimates)
        routes_by_day: dict[int, list[RouteEstimate]] = defaultdict(list)
        if route_result is not None:
            for route in route_result.routes:
                routes_by_day[route.day_index].append(route)

        for position, day in enumerate(rebuilt.days):
            day_index = day.day_index if day.day_index >= 0 else position
            day_routes = sorted(
                routes_by_day.get(day_index, []),
                key=lambda item: (_LEG_ORDER[item.leg_type], item.leg_index),
            )
            day.description = self._day_description(day.date, day.attractions)
            day.transportation = self._transportation_description(
                request,
                day.attractions,
                day_routes,
                existing_transportation=day.transportation,
            )
            day.accommodation = self._accommodation_description(
                request,
                day.hotel,
            )
            start_hotel, return_hotel = resolve_day_hotels(rebuilt, position)
            day.meals = self._build_meals(
                request,
                day.attractions,
                start_hotel=start_hotel,
                return_hotel=return_hotel,
            )

        rebuilt.budget = self._build_budget(rebuilt, route_result)
        rebuilt.overall_suggestions = self._overall_suggestions(
            request,
            rebuilt,
            schedule_quality_report,
        )
        return rebuilt

    @staticmethod
    def _day_description(date: str, attractions: list[Any]) -> str:
        if not attractions:
            return f"{date} \u6682\u65e0\u56fa\u5b9a\u666f\u70b9\u5b89\u6392\uff0c\u53ef\u5728\u9152\u5e97\u6216\u5e02\u533a\u5468\u8fb9\u81ea\u7531\u6d3b\u52a8\u5e76\u9884\u7559\u4f11\u606f\u65f6\u95f4\u3002"
        names = "\u3001".join(item.name for item in attractions)
        total_minutes = sum(max(0, item.visit_duration) for item in attractions)
        return f"{date} \u6309\u987a\u5e8f\u6e38\u89c8{names}\uff0c\u666f\u70b9\u6e38\u89c8\u65f6\u95f4\u5408\u8ba1\u7ea6{total_minutes}\u5206\u949f\u3002"

    @staticmethod
    def _transportation_description(
        request: TripRequest,
        attractions: list[Any],
        routes: list[RouteEstimate],
        *,
        existing_transportation: str | None = None,
    ) -> str:
        # 保留当前路线快照实际使用的交通方式，避免仅重建文案就让已有路线失效。
        effective_mode = normalize_transportation_mode(
            existing_transportation or request.transportation
        )
        requested_mode = _MODE_LABELS[effective_mode]
        usable = [
            item
            for item in routes
            if item.available and item.duration_seconds is not None
        ]
        if usable:
            segments = []
            for item in usable:
                minutes = max(1, math.ceil((item.duration_seconds or 0) / 60))
                distance = (
                    f"\uff0c\u7ea6{item.distance_meters / 1000:.1f}\u516c\u91cc"
                    if item.distance_meters is not None
                    else ""
                )
                segments.append(
                    f"{item.origin_name}\u2192{item.destination_name}"
                    f"\uff08{_MODE_LABELS[item.mode]}\u7ea6{minutes}\u5206\u949f{distance}\uff09"
                )
            return (
                f"\u51fa\u884c\u65b9\u5f0f\uff1a{requested_mode}\uff1b"
                + "\uff1b".join(segments)
                + "\u3002"
            )

        mode = requested_mode
        if attractions:
            names = "\u2192".join(item.name for item in attractions)
            return f"\u5efa\u8bae\u4ee5{mode}\u4f9d\u6b21\u524d\u5f80{names}\uff0c\u51fa\u53d1\u524d\u518d\u6b21\u786e\u8ba4\u5b9e\u65f6\u8def\u7ebf\u3002"
        return f"\u5f53\u65e5\u65e0\u56fa\u5b9a\u666f\u70b9\u8def\u7ebf\uff0c\u5e02\u5185\u6d3b\u52a8\u5efa\u8bae\u4f7f\u7528{mode}\u3002"

    @staticmethod
    def _accommodation_description(request: TripRequest, hotel: Hotel | None) -> str:
        if hotel is None:
            return f"\u5f53\u65e5\u672a\u7ed1\u5b9a\u5177\u4f53\u9152\u5e97\uff0c\u6309\u201c{request.accommodation}\u201d\u504f\u597d\u81ea\u884c\u786e\u8ba4\u4f4f\u5bbf\u6216\u8fd4\u7a0b\u3002"
        cost = f"\uff0c\u9884\u8ba1{hotel.estimated_cost}\u5143/\u665a" if hotel.estimated_cost > 0 else ""
        return f"\u5165\u4f4f{hotel.name}\uff08{hotel.address or '\u5730\u5740\u4ee5\u9884\u8ba2\u4fe1\u606f\u4e3a\u51c6'}{cost}\uff09\u3002"

    @staticmethod
    def _build_meals(
        request: TripRequest,
        attractions: list[Any],
        *,
        start_hotel: Hotel | None,
        return_hotel: Hotel | None,
    ) -> list[Meal]:
        middle = attractions[len(attractions) // 2] if attractions else None
        last = attractions[-1] if attractions else None
        breakfast_anchor = start_hotel or middle
        lunch_anchor = middle or start_hotel or return_hotel
        dinner_anchor = return_hotel or last or start_hotel
        anchors = {
            "breakfast": breakfast_anchor,
            "lunch": lunch_anchor,
            "dinner": dinner_anchor,
        }
        labels = {"breakfast": "\u65e9\u9910", "lunch": "\u5348\u9910", "dinner": "\u665a\u9910"}
        meals: list[Meal] = []
        for meal_type in ("breakfast", "lunch", "dinner"):
            anchor = anchors[meal_type]
            anchor_name = anchor.name if anchor is not None else f"{request.city}\u5e02\u533a"
            meals.append(
                Meal(
                    type=meal_type,
                    name=f"{anchor_name}\u9644\u8fd1{labels[meal_type]}",
                    address=(getattr(anchor, "address", None) if anchor is not None else None),
                    location=(
                        anchor.location.model_copy(deep=True)
                        if anchor is not None and anchor.location is not None
                        else None
                    ),
                    description=f"\u56f4\u7ed5\u5f53\u5929\u5b9e\u9645\u5730\u70b9\u5b89\u6392{labels[meal_type]}\uff0c\u4f18\u5148\u9009\u62e9\u6b65\u884c\u53ef\u8fbe\u4e14\u660e\u7801\u6807\u4ef7\u7684\u9910\u9986\u3002",
                    estimated_cost=_MEAL_COSTS[meal_type],
                )
            )
        return meals

    @staticmethod
    def _build_budget(
        plan: TripPlan,
        route_result: RouteEstimateResult | None,
    ) -> Budget:
        attractions = sum(
            max(0, item.ticket_price)
            for day in plan.days
            for item in day.attractions
        )
        hotels = sum(
            max(0, day.hotel.estimated_cost)
            for day in plan.days
            if day.hotel is not None
        )
        meals = sum(
            max(0, meal.estimated_cost)
            for day in plan.days
            for meal in day.meals
        )
        transportation = sum(
            TripPlanConsistencyRebuilder._route_cost(item)
            for item in (route_result.routes if route_result is not None else [])
        )
        total = attractions + hotels + meals + transportation
        return Budget(
            total_attractions=attractions,
            total_hotels=hotels,
            total_meals=meals,
            total_transportation=transportation,
            total=total,
        )

    @staticmethod
    def _route_cost(route: RouteEstimate) -> int:
        if not route.available or route.distance_meters is None:
            return 0
        distance_km = route.distance_meters / 1000
        if route.mode == "walking":
            return 0
        if route.mode == "transit":
            return max(2, math.ceil(2 + distance_km * 0.45))
        return max(12, math.ceil(12 + distance_km * 2.4))

    @staticmethod
    def _overall_suggestions(
        request: TripRequest,
        plan: TripPlan,
        schedule_quality_report: ScheduleQualityReport | dict[str, Any] | None,
    ) -> str:
        names = [item.name for day in plan.days for item in day.attractions]
        if names:
            joined_names = "\u3001".join(names)
            content = f"\u672c\u6b21\u884c\u7a0b\u5305\u542b{joined_names}\uff0c\u8bf7\u63d0\u524d\u6838\u5bf9\u5f00\u653e\u65f6\u95f4\u5e76\u6309\u6700\u7ec8\u987a\u5e8f\u51fa\u884c\u3002"
        else:
            content = "\u5f53\u524d\u6ca1\u6709\u53ef\u6267\u884c\u7684\u56fa\u5b9a\u666f\u70b9\uff0c\u8bf7\u8865\u5145\u5019\u9009\u666f\u70b9\u540e\u518d\u51fa\u53d1\u3002"
        schedule = _schedule_report(schedule_quality_report)
        if schedule is not None and schedule.total_overtime_minutes == 0:
            content += " \u5f53\u524d\u65f6\u95f4\u8f74\u65e0\u8d85\u65f6\uff0c\u4ecd\u5efa\u8bae\u6bcf\u5929\u9884\u7559\u673a\u52a8\u65f6\u95f4\u3002"
        content += f" \u5e02\u5185\u4ea4\u901a\u6309\u201c{request.transportation}\u201d\u89c4\u5212\uff0c\u8d39\u7528\u4e0e\u73ed\u6b21\u4ee5\u51fa\u884c\u5f53\u65e5\u5b9e\u65f6\u4fe1\u606f\u4e3a\u51c6\u3002"
        return content


def _route_result(
    value: RouteEstimateResult | dict[str, Any] | None,
) -> RouteEstimateResult | None:
    if value is None:
        return None
    return value if isinstance(value, RouteEstimateResult) else RouteEstimateResult.model_validate(value)


def _schedule_report(
    value: ScheduleQualityReport | dict[str, Any] | None,
) -> ScheduleQualityReport | None:
    if value is None:
        return None
    return value if isinstance(value, ScheduleQualityReport) else ScheduleQualityReport.model_validate(value)


def _hotel_source(hotel: Hotel | None) -> dict[str, Any] | None:
    if hotel is None:
        return None
    return {
        "name": hotel.name,
        "address": hotel.address,
        "estimated_cost": hotel.estimated_cost,
        "longitude": round(hotel.location.longitude, 6) if hotel.location else None,
        "latitude": round(hotel.location.latitude, 6) if hotel.location else None,
    }
