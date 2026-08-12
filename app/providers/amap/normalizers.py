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
    LocationResolutionResult,
    PlaceCandidate,
    PoiCandidate,
    PoiDetailResult,
    PoiSearchResult,
    RestaurantCandidate,
    RestaurantSearchAnchor,
    RestaurantSearchResult,
    RestaurantSearchSnapshot,
    RouteEstimate,
    RouteLegRequest,
    RouteMode,
    WeatherForecast,
    WeatherSearchResult,
)


CandidateT = TypeVar("CandidateT", bound=PlaceCandidate)
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


def _nonnegative_int(value: Any) -> int | None:
    number = _nonnegative_number(value)
    if number is None:
        return None
    return max(0, round(number))


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
    """兼容 POI v5 的 business 与旧版 v3 的 biz_ext。"""

    for key in ("business", "biz_ext"):
        value = poi.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _opening_hours(raw: dict[str, Any], business: dict[str, Any]) -> str:
    return _text(
        business.get(
            "opentime_today",
            business.get(
                "opentime_week",
                business.get("opentime", raw.get("opentime", "")),
            ),
        )
    )


def _poi_candidate(raw: dict[str, Any]) -> PoiCandidate | None:
    name = _text(raw.get("name"))
    address = _text(raw.get("address"))
    location = _location(raw.get("location"))
    if not name or location is None:
        return None
    business = _business_extension(raw)
    return PoiCandidate(
        poi_id=_text(raw.get("id")),
        name=name,
        address=address or _text(raw.get("adname")) or "地址待确认",
        location=location,
        district=_text(raw.get("adname")),
        city_code=_text(raw.get("citycode")),
        adcode=_text(raw.get("adcode")),
        rating=_rating(business.get("rating", raw.get("rating"))),
        telephone=_text(raw.get("tel")),
        category=_text(raw.get("type")),
        type_code=_text(raw.get("typecode")),
        opening_hours=_opening_hours(raw, business),
        average_cost=_nonnegative_number(
            business.get("cost", raw.get("cost", raw.get("price")))
        ),
        distance_meters=_nonnegative_int(raw.get("distance")),
    )


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


def normalize_pois(
    payload: dict[str, Any],
    *,
    city: str,
    keywords: str,
    types: str,
    limit: int,
    center: GeoPoint | None = None,
    radius_meters: int | None = None,
) -> PoiSearchResult:
    """统一处理 POI v5 文本/周边搜索结果。"""

    raw_pois = payload.get("pois")
    pois = raw_pois if isinstance(raw_pois, list) else []
    candidates = [
        candidate
        for raw in pois
        if isinstance(raw, dict)
        for candidate in [_poi_candidate(raw)]
        if candidate is not None
    ]
    return PoiSearchResult(
        query_city=city,
        keywords=keywords,
        types=types,
        center=center,
        radius_meters=radius_meters,
        total_received=len(pois),
        candidates=_dedupe_sort_crop(candidates, limit=limit),
    )


def normalize_restaurant_snapshot(
    payload: dict[str, Any],
    *,
    city: str,
    keywords: str,
    center: GeoPoint,
    radius_meters: int,
    page_size: int,
) -> RestaurantSearchSnapshot:
    """把一次高德周边查询转成可跨会话缓存的稳定 POI 快照。"""

    pois = payload.get("pois") if isinstance(payload.get("pois"), list) else []
    candidates = [
        candidate
        for raw in pois
        if isinstance(raw, dict)
        for candidate in [_poi_candidate(raw)]
        if candidate is not None
    ]
    # 周边餐饮按距离优先去重；评分仅用于相同距离候选的稳定排序。
    unique: dict[str, PoiCandidate] = {}
    for candidate in candidates:
        identity = (candidate.poi_id or f"{candidate.name}|{candidate.address}").lower()
        existing = unique.get(identity)
        if existing is None:
            unique[identity] = candidate
            continue
        old_distance = existing.distance_meters if existing.distance_meters is not None else 10**9
        new_distance = candidate.distance_meters if candidate.distance_meters is not None else 10**9
        if new_distance < old_distance:
            unique[identity] = candidate
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.distance_meters if item.distance_meters is not None else 10**9,
            -(item.rating if item.rating is not None else -1),
            item.name,
        ),
    )
    return RestaurantSearchSnapshot(
        query_city=city,
        keywords=keywords,
        center=center,
        radius_meters=radius_meters,
        page_size=max(1, min(25, page_size)),
        total_received=len(pois),
        candidates=ordered[: max(0, page_size)],
    )


def bind_restaurant_snapshot(
    snapshot: RestaurantSearchSnapshot,
    *,
    anchor: RestaurantSearchAnchor,
    limit: int,
) -> RestaurantSearchResult:
    """读取缓存后，把稳定 POI 重新绑定到当前会话的餐次锚点。"""

    candidates = [
        RestaurantCandidate(
            **poi.model_dump(),
            anchor_id=anchor.anchor_id,
            day_index=anchor.day_index,
            meal_type=anchor.meal_type,
        )
        for poi in snapshot.candidates[: max(0, limit)]
    ]
    return RestaurantSearchResult(
        query_city=snapshot.query_city,
        keywords=snapshot.keywords,
        requested_anchors=1,
        searched_anchors=1,
        total_received=snapshot.total_received,
        candidates=candidates,
    )


def normalize_restaurants(
    payload: dict[str, Any],
    *,
    city: str,
    keywords: str,
    anchor: RestaurantSearchAnchor,
    limit: int,
) -> RestaurantSearchResult:
    """兼容旧调用：先创建稳定快照，再绑定到具体餐次锚点。"""

    snapshot = normalize_restaurant_snapshot(
        payload,
        city=city,
        keywords=keywords,
        center=anchor.location,
        radius_meters=3000,
        page_size=max(1, limit),
    )
    return bind_restaurant_snapshot(snapshot, anchor=anchor, limit=limit)

def normalize_poi_detail(payload: dict[str, Any], *, poi_id: str) -> PoiDetailResult:
    """POI 详情不存在时返回 found=false，而不是制造不完整地点。"""

    pois = payload.get("pois") if isinstance(payload.get("pois"), list) else []
    candidate = next(
        (
            normalized
            for item in pois
            if isinstance(item, dict)
            for normalized in [_poi_candidate(item)]
            if normalized is not None
        ),
        None,
    )
    return PoiDetailResult(
        poi_id=poi_id,
        found=candidate is not None,
        candidate=candidate,
    )


def normalize_geocode_location(
    payload: dict[str, Any], *, query: str, city: str
) -> LocationResolutionResult:
    """把地理编码首个有效结果转成统一地点解析输出。"""

    geocodes = payload.get("geocodes") if isinstance(payload.get("geocodes"), list) else []
    for raw in geocodes:
        if not isinstance(raw, dict):
            continue
        location = _location(raw.get("location"))
        if location is None:
            continue
        candidate = PoiCandidate(
            name=query,
            address=_text(raw.get("formatted_address")) or query,
            location=location,
            district=_text(raw.get("district")),
            city_code=_text(raw.get("citycode")),
            adcode=_text(raw.get("adcode")),
            category="geocode",
        )
        return LocationResolutionResult(
            query=query,
            city=city,
            resolved=True,
            source="geocode",
            confidence=0.72,
            candidate=candidate,
        )
    return LocationResolutionResult(query=query, city=city)


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
                opening_hours=_opening_hours(raw, biz_ext),
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



def normalize_city_code(payload: dict[str, Any]) -> str:
    """从高德行政区查询结果中提取 citycode。"""

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
    """把高德路线规划 2.0 的首选路径转换成稳定路线指标。"""

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
            leg_type=leg.leg_type,
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
            leg_type=leg.leg_type,
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
        leg_type=leg.leg_type,
        date=leg.date,
        origin_name=leg.origin.name,
        destination_name=leg.destination.name,
        mode=mode,
        available=True,
        distance_meters=distance,
        duration_seconds=duration,
    )
