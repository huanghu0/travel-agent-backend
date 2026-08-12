"""根据最终行程构造真实餐饮搜索锚点。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.providers.amap.models import GeoPoint, RestaurantSearchAnchor
from app.routing.plan_routes import resolve_day_hotels
from app.schemas.trip_schema import TripPlan


def _anchor_location(place: Any) -> GeoPoint | None:
    """把酒店或景点坐标转换为 Provider 层统一坐标模型。"""

    location = getattr(place, "location", None)
    if location is None:
        return None
    return GeoPoint(
        longitude=location.longitude,
        latitude=location.latitude,
    )


def build_restaurant_search_anchors(
    plan: TripPlan,
    *,
    max_anchors: int,
) -> list[RestaurantSearchAnchor]:
    """为每天早餐、午餐和晚餐选择确定性附近搜索位置。

    早餐优先使用出发酒店，午餐使用当天中间景点，晚餐优先使用返回
    酒店。地点缺少坐标时跳过，最终通过固定优先级裁剪，避免多日行程产生
    无界高德请求。
    """

    if max_anchors <= 0:
        return []

    by_type: dict[str, list[RestaurantSearchAnchor]] = {
        "breakfast": [],
        "lunch": [],
        "dinner": [],
    }
    for day_position, day in enumerate(plan.days):
        start_hotel, return_hotel = resolve_day_hotels(plan, day_position)
        first = day.attractions[0] if day.attractions else None
        middle = (
            day.attractions[len(day.attractions) // 2]
            if day.attractions
            else None
        )
        last = day.attractions[-1] if day.attractions else None
        places = {
            "breakfast": start_hotel or first or middle,
            "lunch": middle or start_hotel or return_hotel,
            "dinner": return_hotel or last or start_hotel,
        }
        for meal_type, place in places.items():
            location = _anchor_location(place)
            name = str(getattr(place, "name", "") or "").strip()
            if location is None or not name:
                continue
            by_type[meal_type].append(
                RestaurantSearchAnchor(
                    anchor_id=f"day-{day.day_index}-{meal_type}",
                    day_index=day.day_index,
                    meal_type=meal_type,
                    name=name,
                    location=location,
                )
            )

    # 午餐最依赖景点附近真实餐厅，其次保证首日早餐和每日晚餐；剩余容量
    # 再按日期补早餐。该顺序在裁剪时仍能覆盖尽可能多的行程日。
    ordered: list[RestaurantSearchAnchor] = []
    ordered.extend(by_type["lunch"])
    if by_type["breakfast"]:
        ordered.append(by_type["breakfast"][0])
    ordered.extend(by_type["dinner"])
    ordered.extend(by_type["breakfast"][1:])
    return ordered[:max_anchors]


def restaurant_search_source_fingerprint(
    plan: TripPlan,
    *,
    max_anchors: int,
) -> str:
    """对真实餐饮搜索实际依赖的锚点生成稳定指纹。"""

    anchors = build_restaurant_search_anchors(plan, max_anchors=max_anchors)
    payload = [anchor.model_dump(mode="json") for anchor in anchors]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
