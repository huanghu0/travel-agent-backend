"""Build a bounded list of adjacent-attraction route legs from the final TripPlan."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.providers.amap.models import RouteLegRequest, RouteMode, RoutePoint
from app.schemas.trip_schema import Attraction, TripPlan, TripRequest


def normalize_transportation_mode(value: str | None) -> RouteMode:
    """Map free-form Chinese or English transport labels to Amap route modes."""

    text = (value or "").strip().lower()
    # Transit wins for combined descriptions such as walking plus metro.
    if any(marker in text for marker in (
        "\u516c\u5171\u4ea4\u901a", "\u516c\u4ea4", "\u5730\u94c1", "\u8f68\u9053", "\u8f7b\u8f68",
        "public", "transit", "metro", "subway", "bus",
    )):
        return "transit"
    if any(marker in text for marker in (
        "\u81ea\u9a7e", "\u9a7e\u8f66", "\u5f00\u8f66", "\u6c7d\u8f66", "\u6253\u8f66", "\u51fa\u79df\u8f66",
        "driving", "drive", "car", "taxi",
    )):
        return "driving"
    if any(marker in text for marker in (
        "\u6b65\u884c", "\u5f92\u6b65", "walking", "walk", "hiking",
    )):
        return "walking"
    # Urban travel defaults to transit rather than silently assuming car access.
    return "transit"


def plan_route_fingerprint(request: TripRequest, plan: TripPlan) -> str:
    """Hash only fields that affect route results, avoiding duplicate Amap calls."""

    payload = {
        "request_transportation": request.transportation,
        "days": [
            {
                "date": day.date,
                "day_index": day.day_index,
                "transportation": day.transportation,
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


def _source_candidates(attractions: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(attractions, dict):
        return []
    candidates = attractions.get("candidates")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]
    # Compatibility with checkpoints created by the retired map agents.
    pois = attractions.get("pois")
    return [item for item in pois if isinstance(item, dict)] if isinstance(pois, list) else []


def _source_indexes(
    attractions: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    fallback_city_code = ""
    for item in _source_candidates(attractions):
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
    attraction: Attraction,
    *,
    by_id: dict[str, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
    fallback_city_code: str,
) -> RoutePoint:
    source = None
    if attraction.poi_id:
        source = by_id.get(_normalized_text(attraction.poi_id))
    if source is None:
        source = by_name.get(_normalized_text(attraction.name))
    source = source or {}
    poi_id = attraction.poi_id or str(source.get("poi_id", source.get("id", "")) or "")
    city_code = str(
        source.get("city_code", source.get("citycode", fallback_city_code)) or ""
    ).strip()
    return RoutePoint(
        name=attraction.name,
        location={
            "longitude": attraction.location.longitude,
            "latitude": attraction.location.latitude,
        },
        poi_id=poi_id,
        city_code=city_code,
    )


def build_route_legs(
    request: TripRequest,
    plan: TripPlan,
    *,
    attractions: dict[str, Any] | None = None,
) -> list[RouteLegRequest]:
    """Build adjacent attraction legs only; never calculate a full POI matrix."""

    by_id, by_name, fallback_city_code = _source_indexes(attractions)
    legs: list[RouteLegRequest] = []
    for day_position, day in enumerate(plan.days):
        mode = normalize_transportation_mode(day.transportation or request.transportation)
        stable_day_index = day.day_index if day.day_index >= 0 else day_position
        for leg_index in range(max(0, len(day.attractions) - 1)):
            origin = _route_point(
                day.attractions[leg_index],
                by_id=by_id,
                by_name=by_name,
                fallback_city_code=fallback_city_code,
            )
            destination = _route_point(
                day.attractions[leg_index + 1],
                by_id=by_id,
                by_name=by_name,
                fallback_city_code=fallback_city_code,
            )
            legs.append(
                RouteLegRequest(
                    day_index=stable_day_index,
                    leg_index=leg_index,
                    date=day.date,
                    origin=origin,
                    destination=destination,
                    mode=mode,
                )
            )
    return legs
