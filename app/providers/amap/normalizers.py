"""把高德原始 JSON 转换成稳定、紧凑的业务模型。"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, TypeVar

from app.providers.amap.models import (
    AttractionCandidate,
    AttractionSearchResult,
    GeoPoint,
    HotelCandidate,
    HotelSearchResult,
    RouteEstimate,
    RouteLegRequest,
    RouteMode,
    WeatherForecast,
    WeatherSearchResult,
)


CandidateT = TypeVar("CandidateT", AttractionCandidate, HotelCandidate)
_WHITESPACE = re.compile(r"\s+")


def _text(value: Any) -> str:
    """高德空字段有时返回 []；这里只保留紧凑的单行字符串。"""

    if isinstance(value, str):
        return _WHITESPACE.sub(" ", value).strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ",".join(item for item in (_text(item) for item in value) if item)
    return ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, "", []):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rating(value: Any) -> float | None:
    rating = _number(value)
    if rating is None or not 0 <= rating <= 5:
        return None
    return rating


def _nonnegative_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _location(value: Any) -> GeoPoint | None:
    """兼容高德的 '经度,纬度'、数组和对象三种坐标表示。"""

    longitude: Any = None
    latitude: Any = None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) == 2:
            longitude, latitude = parts
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        longitude, latitude = value[0], value[1]
    elif isinstance(value, dict):
        longitude = value.get("longitude", value.get("lng", value.get("lon")))
        latitude = value.get("latitude", value.get("lat"))

    lon_number = _number(longitude)
    lat_number = _number(latitude)
    if lon_number is None or lat_number is None:
        return None
    if not -180 <= lon_number <= 180 or not -90 <= lat_number <= 90:
        return None
    return GeoPoint(longitude=lon_number, latitude=lat_number)


def _business_extension(poi: dict[str, Any]) -> dict[str, Any]:
    value = poi.get("biz_ext")
    return value if isinstance(value, dict) else {}


def _dedupe_sort_crop(
    candidates: Iterable[CandidateT],
    *,
    limit: int,
) -> list[CandidateT]:
    """按 ID/名称地址去重，评分优先且保持供应商原始顺序稳定。"""

    by_key: dict[str, tuple[int, CandidateT]] = {}
    for index, candidate in enumerate(candidates):
        fallback = f"{candidate.name}|{candidate.address}".lower()
        key = candidate.poi_id.lower() if candidate.poi_id else fallback
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = (index, candidate)
            continue
        existing_rating = existing[1].rating if existing[1].rating is not None else -1
        candidate_rating = candidate.rating if candidate.rating is not None else -1
        if candidate_rating > existing_rating:
            by_key[key] = (existing[0], candidate)

    unique = list(by_key.values())
    unique.sort(
        key=lambda item: (
            -(item[1].rating if item[1].rating is not None else -1),
            item[0],
        )
    )
    return [candidate for _, candidate in unique[: max(0, limit)]]


def normalize_attractions(
    payload: dict[str, Any],
    *,
    city: str,
    keywords: str,
    limit: int,
) -> AttractionSearchResult:
    """过滤缺名称/地址/坐标的 POI，并输出有限数量的景点候选。"""

    raw_pois = payload.get("pois")
    pois = raw_pois if isinstance(raw_pois, list) else []
    candidates: list[AttractionCandidate] = []
    for raw in pois:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"))
        address = _text(raw.get("address"))
        location = _location(raw.get("location"))
        if not name or not address or location is None:
            continue
        biz_ext = _business_extension(raw)
        candidates.append(
            AttractionCandidate(
                poi_id=_text(raw.get("id")),
                name=name,
                address=address,
                location=location,
                category=_text(raw.get("type")),
                district=_text(raw.get("adname")),
                city_code=_text(raw.get("citycode")),
                adcode=_text(raw.get("adcode")),
                rating=_rating(biz_ext.get("rating", raw.get("rating"))),
                telephone=_text(raw.get("tel")),
            )
        )

    return AttractionSearchResult(
        query_city=city,
        keywords=keywords,
        total_received=len(pois),
        candidates=_dedupe_sort_crop(candidates, limit=limit),
    )


def normalize_hotels(
    payload: dict[str, Any],
    *,
    city: str,
    keywords: str,
    limit: int,
) -> HotelSearchResult:
    """将酒店 POI 转为统一字段，并按评分去重、排序和裁剪。"""

    raw_pois = payload.get("pois")
    pois = raw_pois if isinstance(raw_pois, list) else []
    candidates: list[HotelCandidate] = []
    for raw in pois:
        if not isinstance(raw, dict):
            continue
        name = _text(raw.get("name"))
        address = _text(raw.get("address"))
        location = _location(raw.get("location"))
        if not name or not address or location is None:
            continue
        biz_ext = _business_extension(raw)
        candidates.append(
            HotelCandidate(
                poi_id=_text(raw.get("id")),
                name=name,
                address=address,
                location=location,
                type=_text(raw.get("type")),
                district=_text(raw.get("adname")),
                city_code=_text(raw.get("citycode")),
                adcode=_text(raw.get("adcode")),
                rating=_rating(biz_ext.get("rating", raw.get("rating"))),
                telephone=_text(raw.get("tel")),
                estimated_cost=_nonnegative_number(
                    biz_ext.get("cost", raw.get("cost", raw.get("price")))
                ),
            )
        )

    return HotelSearchResult(
        query_city=city,
        keywords=keywords,
        total_received=len(pois),
        candidates=_dedupe_sort_crop(candidates, limit=limit),
    )


def normalize_weather(
    payload: dict[str, Any],
    *,
    city: str,
    limit: int,
) -> WeatherSearchResult:
    """展平高德 forecasts/casts 嵌套，只保留规划需要的逐日字段。"""

    raw_forecasts = payload.get("forecasts")
    forecast_groups = raw_forecasts if isinstance(raw_forecasts, list) else []
    group = next((item for item in forecast_groups if isinstance(item, dict)), {})
    raw_casts = group.get("casts") if isinstance(group, dict) else []
    casts = raw_casts if isinstance(raw_casts, list) else []

    forecasts: list[WeatherForecast] = []
    seen_dates: set[str] = set()
    for raw in casts:
        if not isinstance(raw, dict):
            continue
        forecast_date = _text(raw.get("date"))
        try:
            date.fromisoformat(forecast_date)
        except ValueError:
            continue
        if forecast_date in seen_dates:
            continue
        seen_dates.add(forecast_date)
        forecasts.append(
            WeatherForecast(
                date=forecast_date,
                day_weather=_text(raw.get("dayweather")),
                night_weather=_text(raw.get("nightweather")),
                day_temp=_number(raw.get("daytemp_float", raw.get("daytemp"))),
                night_temp=_number(raw.get("nighttemp_float", raw.get("nighttemp"))),
                day_wind_direction=_text(raw.get("daywind")),
                night_wind_direction=_text(raw.get("nightwind")),
                day_wind_power=_text(raw.get("daypower")),
                night_wind_power=_text(raw.get("nightpower")),
            )
        )

    forecasts.sort(key=lambda item: item.date)
    return WeatherSearchResult(
        query_city=city,
        city=_text(group.get("city")) if isinstance(group, dict) else "",
        province=_text(group.get("province")) if isinstance(group, dict) else "",
        report_time=_text(group.get("reporttime")) if isinstance(group, dict) else "",
        forecasts=forecasts[: max(0, limit)],
    )



def _nonnegative_int(value: Any) -> int | None:
    number = _nonnegative_number(value)
    if number is None:
        return None
    return max(0, round(number))


def normalize_city_code(payload: dict[str, Any]) -> str:
    """Extract citycode from an Amap district lookup response."""

    districts = payload.get("districts")
    if not isinstance(districts, list):
        return ""
    for raw in districts:
        if not isinstance(raw, dict):
            continue
        city_code = _text(raw.get("citycode"))
        if city_code:
            return city_code
    return ""


def normalize_route(
    payload: dict[str, Any],
    *,
    leg: RouteLegRequest,
    mode: RouteMode,
) -> RouteEstimate:
    """Normalize the preferred Route Planning 2.0 path into stable metrics."""

    route = payload.get("route")
    route_object = route if isinstance(route, dict) else {}
    collection_name = "transits" if mode == "transit" else "paths"
    raw_routes = route_object.get(collection_name)
    candidates = raw_routes if isinstance(raw_routes, list) else []
    candidate = next((item for item in candidates if isinstance(item, dict)), None)
    if candidate is None:
        return RouteEstimate(
            day_index=leg.day_index,
            leg_index=leg.leg_index,
            date=leg.date,
            origin_name=leg.origin.name,
            destination_name=leg.destination.name,
            mode=mode,
            available=False,
            error_code="NO_ROUTE",
            error_message="Amap returned no available route",
        )

    cost = candidate.get("cost")
    cost_object = cost if isinstance(cost, dict) else {}
    distance = _nonnegative_int(candidate.get("distance"))
    duration = _nonnegative_int(
        cost_object.get("duration", candidate.get("duration"))
    )
    if distance is None or duration is None:
        return RouteEstimate(
            day_index=leg.day_index,
            leg_index=leg.leg_index,
            date=leg.date,
            origin_name=leg.origin.name,
            destination_name=leg.destination.name,
            mode=mode,
            available=False,
            error_code="INVALID_ROUTE_METRICS",
            error_message="Amap route is missing valid distance or duration",
        )

    return RouteEstimate(
        day_index=leg.day_index,
        leg_index=leg.leg_index,
        date=leg.date,
        origin_name=leg.origin.name,
        destination_name=leg.destination.name,
        mode=mode,
        available=True,
        distance_meters=distance,
        duration_seconds=duration,
    )
