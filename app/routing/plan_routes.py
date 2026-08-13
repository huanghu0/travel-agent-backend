"""为行程中的酒店和景点构建确定性的路线分段。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.providers.amap.models import (
    RouteLegRequest,
    RouteLegType,
    RouteMode,
    RoutePoint,
)
from app.schemas.trip_schema import Attraction, Hotel, TripPlan, TripRequest


RouteLegKey = tuple[int, RouteLegType, int]
RoutablePlace = Attraction | Hotel


_MODE_MARKERS: tuple[tuple[RouteMode, tuple[str, ...]], ...] = (
    (
        "transit",
        (
            "\u516c\u5171\u4ea4\u901a",
            "\u516c\u4ea4",
            "\u5730\u94c1",
            "\u8f68\u9053",
            "\u8f7b\u8f68",
            "public",
            "transit",
            "metro",
            "subway",
            "bus",
        ),
    ),
    (
        "driving",
        (
            "\u81ea\u9a7e",
            "\u9a7e\u8f66",
            "\u5f00\u8f66",
            "\u6c7d\u8f66",
            "\u6253\u8f66",
            "\u51fa\u79df\u8f66",
            "driving",
            "drive",
            "car",
            "taxi",
        ),
    ),
    ("walking", ("\u6b65\u884c", "\u5f92\u6b65", "walking", "walk", "hiking")),
)


def _match_transportation_mode(text: str) -> RouteMode | None:
    """从一段已经规范化的文本中识别交通模式。"""

    for mode, markers in _MODE_MARKERS:
        if any(marker in text for marker in markers):
            return mode
    return None


def normalize_transportation_mode(value: str | None) -> RouteMode:
    """把中英文自由文本交通方式映射成高德路线模式。"""

    text = (value or "").strip().lower()

    # 内容重建后的文案可能同时包含“主交通方式”和个别路线分段方式。
    # 显式声明应优先于后文分段，避免驾车主模式被公交分段误判。
    for prefix in (
        "\u51fa\u884c\u65b9\u5f0f",
        "\u4ea4\u901a\u65b9\u5f0f",
        "travel mode",
        "transportation",
    ):
        prefix_index = text.find(prefix)
        if prefix_index < 0:
            continue
        declared = text[prefix_index + len(prefix):].lstrip(" \t:\uff1a")
        declared = (
            declared.split("\uff1b", 1)[0]
            .split(";", 1)[0]
            .split("\u3002", 1)[0]
        )
        declared_mode = _match_transportation_mode(declared)
        if declared_mode is not None:
            return declared_mode

    # 没有显式声明时，“步行 + 地铁”等组合描述优先识别为公共交通。
    matched = _match_transportation_mode(text)
    if matched is not None:
        return matched

    # 城市出行默认使用公共交通，不在用户未说明时假设可以驾车。
    return "transit"


def _hotel_fingerprint(hotel: Hotel | None) -> dict[str, Any] | None:
    if hotel is None:
        return None
    return {
        "name": hotel.name,
        "longitude": (
            round(hotel.location.longitude, 6) if hotel.location is not None else None
        ),
        "latitude": (
            round(hotel.location.latitude, 6) if hotel.location is not None else None
        ),
    }


def plan_route_fingerprint(request: TripRequest, plan: TripPlan) -> str:
    """对会影响酒店和景点路线结果的字段生成稳定指纹。"""

    payload = {
        "request_transportation_mode": normalize_transportation_mode(
            request.transportation
        ),
        "days": [
            {
                "date": day.date,
                "day_index": day.day_index,
                "transportation_mode": normalize_transportation_mode(
                    day.transportation or request.transportation
                ),
                "hotel": _hotel_fingerprint(day.hotel),
                "attractions": [
                    {
                        "name": attraction.name,
                        "poi_id": attraction.poi_id or "",
                        "longitude": round(attraction.location.longitude, 6),
                        "latitude": round(attraction.location.latitude, 6),
                    }
                    for attraction in day.attractions
                ],
            }
            for day in plan.days
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_text(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def _source_candidates(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    # 兼容旧版地图智能体已经保存的历史检查点。
    pois = payload.get("pois")
    return [item for item in pois if isinstance(item, dict)] if isinstance(pois, list) else []


def _source_indexes(
    payload: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    fallback_city_code = ""
    for item in _source_candidates(payload):
        poi_id = _normalized_text(item.get("poi_id", item.get("id")))
        name = _normalized_text(item.get("name"))
        if poi_id:
            by_id.setdefault(poi_id, item)
        if name:
            by_name.setdefault(name, item)
        city_code = item.get("city_code", item.get("citycode"))
        if isinstance(city_code, str) and city_code.strip() and not fallback_city_code:
            fallback_city_code = city_code.strip()
    return by_id, by_name, fallback_city_code


def _route_point(
    place: RoutablePlace,
    *,
    by_id: dict[str, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    fallback_city_code: str,
) -> RoutePoint:
    source = None
    place_poi_id = str(getattr(place, "poi_id", "") or "")
    if place_poi_id:
        source = by_id.get(_normalized_text(place_poi_id))
    if source is None:
        source = by_name.get(_normalized_text(place.name))
    source = source or {}
    location = place.location
    if location is None:
        raise ValueError(f"Route endpoint {place.name!r} has no location")
    poi_id = place_poi_id or str(source.get("poi_id", source.get("id", "")) or "")
    city_code = str(
        source.get("city_code", source.get("citycode", fallback_city_code)) or ""
    ).strip()
    return RoutePoint(
        name=place.name,
        location={
            "longitude": location.longitude,
            "latitude": location.latitude,
        },
        poi_id=poi_id,
        city_code=city_code,
    )


def resolve_day_hotels(
    plan: TripPlan,
    day_position: int,
) -> tuple[Hotel | None, Hotel | None]:
    """确定性解析某个行程日的出发酒店和返回酒店。

    优先使用当天酒店作为出发点；退房日没有酒店时沿用最近的前序酒店。
    只有当天明确配置酒店时，才生成返回酒店路线。
    """

    day = plan.days[day_position]
    current_hotel = day.hotel if day.hotel is not None and day.hotel.location is not None else None
    start_hotel = current_hotel
    if start_hotel is None:
        for previous_day in reversed(plan.days[:day_position]):
            if previous_day.hotel is not None and previous_day.hotel.location is not None:
                start_hotel = previous_day.hotel
                break
    return start_hotel, current_hotel


def expected_route_leg_keys(plan: TripPlan) -> list[RouteLegKey]:
    """返回完整地点时间轴所要求的精确语义路线键。"""

    keys: list[RouteLegKey] = []
    for day_position, day in enumerate(plan.days):
        day_index = day.day_index if day.day_index >= 0 else day_position
        start_hotel, return_hotel = resolve_day_hotels(plan, day_position)
        if day.attractions and start_hotel is not None:
            keys.append((day_index, "hotel_departure", 0))
        for leg_index in range(max(0, len(day.attractions) - 1)):
            keys.append((day_index, "between_attractions", leg_index))
        if day.attractions and return_hotel is not None:
            keys.append((day_index, "hotel_return", 0))
    return keys


def build_route_legs(
    request: TripRequest,
    plan: TripPlan,
    *,
    attractions: dict[str, Any] | None = None,
    hotels: dict[str, Any] | None = None,
) -> list[RouteLegRequest]:
    """有界构建酒店出发、相邻景点之间以及返回酒店的路线分段。"""

    attraction_by_id, attraction_by_name, attraction_city_code = _source_indexes(attractions)
    hotel_by_id, hotel_by_name, hotel_city_code = _source_indexes(hotels)
    fallback_city_code = attraction_city_code or hotel_city_code
    legs: list[RouteLegRequest] = []

    for day_position, day in enumerate(plan.days):
        mode = normalize_transportation_mode(day.transportation or request.transportation)
        day_index = day.day_index if day.day_index >= 0 else day_position
        start_hotel, return_hotel = resolve_day_hotels(plan, day_position)

        if day.attractions and start_hotel is not None:
            legs.append(
                RouteLegRequest(
                    day_index=day_index,
                    leg_index=0,
                    leg_type="hotel_departure",
                    date=day.date,
                    origin=_route_point(
                        start_hotel,
                        by_id=hotel_by_id,
                        by_name=hotel_by_name,
                        fallback_city_code=fallback_city_code,
                    ),
                    destination=_route_point(
                        day.attractions[0],
                        by_id=attraction_by_id,
                        by_name=attraction_by_name,
                        fallback_city_code=fallback_city_code,
                    ),
                    mode=mode,
                )
            )

        for leg_index in range(max(0, len(day.attractions) - 1)):
            legs.append(
                RouteLegRequest(
                    day_index=day_index,
                    leg_index=leg_index,
                    leg_type="between_attractions",
                    date=day.date,
                    origin=_route_point(
                        day.attractions[leg_index],
                        by_id=attraction_by_id,
                        by_name=attraction_by_name,
                        fallback_city_code=fallback_city_code,
                    ),
                    destination=_route_point(
                        day.attractions[leg_index + 1],
                        by_id=attraction_by_id,
                        by_name=attraction_by_name,
                        fallback_city_code=fallback_city_code,
                    ),
                    mode=mode,
                )
            )

        if day.attractions and return_hotel is not None:
            legs.append(
                RouteLegRequest(
                    day_index=day_index,
                    leg_index=0,
                    leg_type="hotel_return",
                    date=day.date,
                    origin=_route_point(
                        day.attractions[-1],
                        by_id=attraction_by_id,
                        by_name=attraction_by_name,
                        fallback_city_code=fallback_city_code,
                    ),
                    destination=_route_point(
                        return_hotel,
                        by_id=hotel_by_id,
                        by_name=hotel_by_name,
                        fallback_city_code=fallback_city_code,
                    ),
                    mode=mode,
                )
            )

    return legs
