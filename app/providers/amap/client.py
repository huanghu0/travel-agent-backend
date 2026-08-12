"""高德 HTTP 客户端和标准化 Provider 门面。"""

from __future__ import annotations

import logging
from typing import Any, Callable

import requests

from app.core.config import settings
from app.providers.amap.errors import AmapErrorKind, AmapProviderError, validate_amap_response
from app.providers.amap.models import (
    AttractionSearchResult,
    GeoPoint,
    HotelSearchResult,
    LocationResolutionResult,
    NearbyAttractionSearchResult,
    PoiDetailResult,
    PoiSearchResult,
    RestaurantCandidate,
    RestaurantSearchAnchor,
    RestaurantSearchResult,
    RestaurantSearchSnapshot,
    RouteEstimate,
    RouteEstimateResult,
    RouteLegRequest,
    RouteMode,
    WeatherSearchResult,
)
from app.providers.amap.restaurant_cache import (
    RestaurantCache,
    restaurant_search_cache_key,
)
from app.providers.amap.route_cache import RouteCache, route_leg_cache_key
from app.providers.amap.normalizers import (
    normalize_attractions,
    normalize_city_code,
    normalize_geocode_location,
    normalize_hotels,
    normalize_poi_detail,
    bind_restaurant_snapshot,
    normalize_pois,
    normalize_restaurant_snapshot,
    normalize_route,
    normalize_weather,
)


HttpGet = Callable[..., Any]
logger = logging.getLogger(__name__)


class AmapClient:
    """只负责高德 Web 服务的 HTTP 传输，返回供应商原始 JSON。"""

    http_get: HttpGet = staticmethod(requests.get)

    @classmethod
    def _get_json(cls, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = cls.http_get(
                url,
                params=params,
                timeout=(
                    settings.AMAP_HTTP_CONNECT_TIMEOUT,
                    settings.AMAP_HTTP_READ_TIMEOUT,
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise AmapProviderError(
                "高德地图请求超时",
                kind=AmapErrorKind.TIMEOUT,
                retryable=True,
            ) from exc
        except requests.ConnectionError as exc:
            raise AmapProviderError(
                "无法连接高德地图服务",
                kind=AmapErrorKind.UPSTREAM,
                retryable=True,
            ) from exc
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code in (401, 403):
                kind = AmapErrorKind.AUTHORIZATION
                retryable = False
            elif status_code == 429:
                kind = AmapErrorKind.RATE_LIMIT
                retryable = True
            elif status_code is not None and status_code >= 500:
                kind = AmapErrorKind.UPSTREAM
                retryable = True
            else:
                kind = AmapErrorKind.INVALID_INPUT
                retryable = False
            raise AmapProviderError(
                f"高德地图 HTTP 请求失败（status={status_code or 'unknown'}）",
                kind=kind,
                retryable=retryable,
            ) from exc
        except requests.RequestException as exc:
            raise AmapProviderError(
                "高德地图请求执行失败",
                kind=AmapErrorKind.UPSTREAM,
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise AmapProviderError(
                "高德地图返回了无效 JSON",
                kind=AmapErrorKind.INVALID_OUTPUT,
                retryable=True,
            ) from exc

        if not isinstance(payload, dict):
            raise AmapProviderError(
                "高德地图返回了非对象 JSON",
                kind=AmapErrorKind.INVALID_OUTPUT,
                retryable=True,
            )
        return payload

    @classmethod
    def text_search(
        cls,
        *,
        keywords: str,
        city: str,
        types: str = "",
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """调用高德地点搜索 2.0 关键字搜索。

        `region/city_limit` 是 v5 参数；保留默认参数是为了兼容现有测试中的
        简化 FakeClient，同时让景点、酒店和通用 POI 共用同一传输入口。
        """

        size = page_size or max(
            settings.AMAP_MAX_ATTRACTION_CANDIDATES,
            settings.AMAP_MAX_HOTEL_CANDIDATES,
            settings.AMAP_MAX_POI_CANDIDATES,
            10,
        )
        params: dict[str, Any] = {
            "key": settings.AMAP_API_KEY,
            "keywords": keywords,
            "region": city,
            "city_limit": "true",
            "show_fields": "business",
            "page_size": max(1, min(25, size)),
            "page_num": max(1, page),
        }
        if types:
            params["types"] = types
        return cls._get_json("https://restapi.amap.com/v5/place/text", params)

    @classmethod
    def around_search(
        cls,
        *,
        location: GeoPoint,
        city: str,
        keywords: str,
        radius_meters: int,
        types: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """调用高德地点搜索 2.0 周边搜索，并按距离返回候选。"""

        params: dict[str, Any] = {
            "key": settings.AMAP_API_KEY,
            "location": cls._coordinate(location),
            "keywords": keywords,
            "region": city,
            "city_limit": "true",
            "radius": max(100, min(50000, radius_meters)),
            "sortrule": "distance",
            "show_fields": "business",
            "page_size": max(1, min(25, page_size)),
            "page_num": max(1, page),
        }
        if types:
            params["types"] = types
        return cls._get_json("https://restapi.amap.com/v5/place/around", params)

    @classmethod
    def poi_detail(cls, poi_id: str) -> dict[str, Any]:
        """按 POI ID 查询地点搜索 2.0 详情。"""

        return cls._get_json(
            "https://restapi.amap.com/v5/place/detail",
            {
                "key": settings.AMAP_API_KEY,
                "id": poi_id,
                "show_fields": "business",
            },
        )

    @classmethod
    def geocode(cls, *, address: str, city: str = "") -> dict[str, Any]:
        """把没有稳定 POI 的地址文本解析为高德坐标。"""

        params: dict[str, Any] = {
            "key": settings.AMAP_API_KEY,
            "address": address,
            "output": "json",
        }
        if city:
            params["city"] = city
        return cls._get_json("https://restapi.amap.com/v3/geocode/geo", params)

    @classmethod
    def get_weather(cls, city: str) -> dict[str, Any]:
        return cls._get_json(
            "https://restapi.amap.com/v3/weather/weatherInfo",
            {
                "key": settings.AMAP_API_KEY,
                "city": city,
                "output": "json",
                "extensions": "all",
            },
        )

    @staticmethod
    def _coordinate(point: GeoPoint) -> str:
        """高德坐标参数经度在前，并限制为最多六位小数。"""

        return f"{point.longitude:.6f},{point.latitude:.6f}"

    @classmethod
    def district_search(cls, city: str) -> dict[str, Any]:
        """把城市名称解析成公交路线接口要求的 citycode。"""

        return cls._get_json(
            "https://restapi.amap.com/v3/config/district",
            {
                "key": settings.AMAP_API_KEY,
                "keywords": city,
                "subdistrict": 0,
                "extensions": "base",
                "output": "json",
            },
        )

    @classmethod
    def route(
        cls,
        *,
        origin: GeoPoint,
        destination: GeoPoint,
        mode: RouteMode,
        origin_poi_id: str = "",
        destination_poi_id: str = "",
        origin_city_code: str = "",
        destination_city_code: str = "",
    ) -> dict[str, Any]:
        """调用高德路线规划 2.0 查询一条路线分段。"""

        endpoints = {
            "driving": "https://restapi.amap.com/v5/direction/driving",
            "walking": "https://restapi.amap.com/v5/direction/walking",
            "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
        }
        params: dict[str, Any] = {
            "key": settings.AMAP_API_KEY,
            "origin": cls._coordinate(origin),
            "destination": cls._coordinate(destination),
            "show_fields": "cost",
            "output": "json",
        }
        if mode == "driving":
            params["strategy"] = 32
            if origin_poi_id:
                params["origin_id"] = origin_poi_id
            if destination_poi_id:
                params["destination_id"] = destination_poi_id
        elif mode == "walking":
            if origin_poi_id:
                params["origin_id"] = origin_poi_id
            if destination_poi_id:
                params["destination_id"] = destination_poi_id
        else:
            if not origin_city_code or not destination_city_code:
                raise AmapProviderError(
                    "Transit route requires origin and destination city codes",
                    kind=AmapErrorKind.INVALID_INPUT,
                    retryable=False,
                )
            params.update(
                {
                    "city1": origin_city_code,
                    "city2": destination_city_code,
                    "strategy": 0,
                    "AlternativeRoute": 1,
                }
            )
            # 高德公交路线要求起点和终点 POI ID 成对提供。
            if origin_poi_id and destination_poi_id:
                params["originpoi"] = origin_poi_id
                params["destinationpoi"] = destination_poi_id
        return cls._get_json(endpoints[mode], params)


class AmapProviderClient:
    """高德标准化门面：校验业务状态并输出裁剪后的 Pydantic 模型。"""

    def __init__(
        self,
        raw_client: Any = AmapClient,
        route_cache: RouteCache | None = None,
        restaurant_cache: RestaurantCache | None = None,
    ):
        self.raw_client = raw_client
        self.route_cache = route_cache
        self.restaurant_cache = restaurant_cache
        self._city_code_cache: dict[str, str] = {}

    def search_pois(
        self,
        *,
        city: str,
        keywords: str,
        types: str = "",
        center: GeoPoint | None = None,
        radius_meters: int | None = None,
        limit: int | None = None,
    ) -> PoiSearchResult:
        """搜索任意类型地点，并统一文本搜索与周边搜索输出。"""

        maximum = max(0, limit if limit is not None else settings.AMAP_MAX_POI_CANDIDATES)
        if center is None:
            raw = self.raw_client.text_search(
                keywords=keywords,
                city=city,
                types=types,
                page_size=max(1, maximum),
            )
            radius = None
        else:
            radius = max(100, min(50000, radius_meters or 3000))
            raw = self.raw_client.around_search(
                location=center,
                city=city,
                keywords=keywords,
                types=types,
                radius_meters=radius,
                page_size=max(1, maximum),
            )
        payload = validate_amap_response(raw)
        return normalize_pois(
            payload,
            city=city,
            keywords=keywords,
            types=types,
            center=center,
            radius_meters=radius,
            limit=maximum,
        )

    def search_restaurants(
        self,
        *,
        city: str,
        anchors: list[RestaurantSearchAnchor],
        keywords: str = "餐厅",
        radius_meters: int | None = None,
        max_anchors: int | None = None,
        candidates_per_anchor: int | None = None,
    ) -> RestaurantSearchResult:
        """围绕有限锚点搜索餐厅，并优先复用进程内和 SQLite 快照。

        缓存只保存与锚点无关的 PoiCandidate；读取后再绑定 day_index 和
        meal_type，避免把上一次行程的会话字段错误复用到新行程。
        """

        anchor_limit = max(
            0,
            max_anchors
            if max_anchors is not None
            else settings.AMAP_MAX_RESTAURANT_SEARCH_ANCHORS,
        )
        candidate_limit = min(
            25,
            max(
                0,
                candidates_per_anchor
                if candidates_per_anchor is not None
                else settings.AMAP_MAX_RESTAURANT_CANDIDATES_PER_ANCHOR,
            ),
        )
        radius = max(
            100,
            min(
                50000,
                radius_meters or settings.AMAP_RESTAURANT_SEARCH_RADIUS_METERS,
            ),
        )
        selected = anchors[:anchor_limit]
        snapshots: dict[str, RestaurantSearchSnapshot] = {}
        candidates: list[RestaurantCandidate] = []
        total_received = 0
        cache_hits = 0
        cache_misses = 0

        for anchor in selected:
            cache_key = restaurant_search_cache_key(
                city=city,
                keywords=keywords,
                center=anchor.location,
                radius_meters=radius,
                page_size=max(1, candidate_limit),
            )
            snapshot = snapshots.get(cache_key)
            if snapshot is None:
                snapshot = self._restaurant_cache_get(cache_key)
                if snapshot is not None:
                    cache_hits += 1
                else:
                    if self.restaurant_cache is not None:
                        cache_misses += 1
                    raw = self.raw_client.around_search(
                        location=anchor.location,
                        city=city,
                        keywords=keywords,
                        types="050000",
                        radius_meters=radius,
                        page_size=max(1, candidate_limit),
                    )
                    payload = validate_amap_response(raw)
                    snapshot = normalize_restaurant_snapshot(
                        payload,
                        city=city,
                        keywords=keywords,
                        center=anchor.location,
                        radius_meters=radius,
                        page_size=max(1, candidate_limit),
                    )
                    self._restaurant_cache_set(cache_key, snapshot)
                snapshots[cache_key] = snapshot
                total_received += snapshot.total_received

            normalized = bind_restaurant_snapshot(
                snapshot,
                anchor=anchor,
                limit=candidate_limit,
            )
            candidates.extend(normalized.candidates)

        return RestaurantSearchResult(
            query_city=city,
            keywords=keywords,
            requested_anchors=len(anchors),
            searched_anchors=len(snapshots),
            truncated_anchors=max(0, len(anchors) - len(selected)),
            total_received=total_received,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            candidates=candidates,
        )

    def _restaurant_cache_get(
        self,
        cache_key: str,
    ) -> RestaurantSearchSnapshot | None:
        """缓存异常只降级为实时查询，不能阻断餐饮工具主流程。"""

        if self.restaurant_cache is None:
            return None
        try:
            return self.restaurant_cache.get(cache_key)
        except Exception:
            logger.warning("Restaurant cache read failed", exc_info=True)
            return None

    def _restaurant_cache_set(
        self,
        cache_key: str,
        snapshot: RestaurantSearchSnapshot,
    ) -> None:
        if self.restaurant_cache is None:
            return
        ttl_seconds = settings.AMAP_RESTAURANT_CACHE_TTL_SECONDS
        if ttl_seconds <= 0:
            return
        try:
            self.restaurant_cache.set(
                cache_key,
                snapshot,
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            logger.warning("Restaurant cache write failed", exc_info=True)

    def get_poi_detail(self, poi_id: str) -> PoiDetailResult:
        """获取单个地点详情；不存在时返回 found=false。"""

        raw = self.raw_client.poi_detail(poi_id)
        payload = validate_amap_response(raw)
        return normalize_poi_detail(payload, poi_id=poi_id)

    def resolve_location(self, *, query: str, city: str = "") -> LocationResolutionResult:
        """先用 POI 搜索解析地点，失败后再回退到地理编码。"""

        raw = self.raw_client.text_search(
            keywords=query,
            city=city,
            page_size=min(5, max(1, settings.AMAP_MAX_POI_CANDIDATES)),
        )
        payload = validate_amap_response(raw)
        pois = normalize_pois(
            payload,
            city=city,
            keywords=query,
            types="",
            limit=5,
        )
        if pois.candidates:
            normalized_query = query.strip().casefold()
            candidate = pois.candidates[0]
            confidence = 0.78
            # ?????????????????????????????
            # ???????????????????????????
            exact = next(
                (
                    item
                    for item in pois.candidates
                    if item.name.strip().casefold() == normalized_query
                ),
                None,
            )
            partial = next(
                (
                    item
                    for item in pois.candidates
                    if normalized_query in item.name.strip().casefold()
                    or item.name.strip().casefold() in normalized_query
                ),
                None,
            )
            if exact is not None:
                candidate, confidence = exact, 0.98
            elif partial is not None:
                candidate, confidence = partial, 0.90
            return LocationResolutionResult(
                query=query,
                city=city,
                resolved=True,
                source="poi",
                confidence=confidence,
                candidate=candidate,
            )

        raw_geocode = self.raw_client.geocode(address=query, city=city)
        geocode_payload = validate_amap_response(raw_geocode)
        return normalize_geocode_location(geocode_payload, query=query, city=city)

    def search_attractions(
        self,
        *,
        city: str,
        keywords: str,
    ) -> AttractionSearchResult:
        raw = self.raw_client.text_search(keywords=keywords, city=city)
        payload = validate_amap_response(raw)
        return normalize_attractions(
            payload,
            city=city,
            keywords=keywords,
            limit=settings.AMAP_MAX_ATTRACTION_CANDIDATES,
        )

    def search_nearby_attractions(
        self,
        *,
        city: str,
        keywords: str,
        center: GeoPoint,
        radius_meters: int,
        page: int = 1,
        page_size: int = 20,
    ) -> NearbyAttractionSearchResult:
        raw = self.raw_client.around_search(
            location=center,
            city=city,
            keywords=keywords,
            radius_meters=radius_meters,
            page=page,
            page_size=page_size,
        )
        payload = validate_amap_response(raw)
        normalized = normalize_attractions(
            payload,
            city=city,
            keywords=keywords,
            limit=page_size,
        )
        return NearbyAttractionSearchResult(
            **normalized.model_dump(),
            center=center,
            radius_meters=radius_meters,
            page=page,
            page_size=page_size,
        )

    def search_hotels(
        self,
        *,
        city: str,
        keywords: str = "酒店",
    ) -> HotelSearchResult:
        raw = self.raw_client.text_search(keywords=keywords, city=city)
        payload = validate_amap_response(raw)
        return normalize_hotels(
            payload,
            city=city,
            keywords=keywords,
            limit=settings.AMAP_MAX_HOTEL_CANDIDATES,
        )

    def get_weather(self, city: str) -> WeatherSearchResult:
        raw = self.raw_client.get_weather(city)
        payload = validate_amap_response(raw)
        return normalize_weather(
            payload,
            city=city,
            limit=settings.AMAP_MAX_WEATHER_DAYS,
        )


    def resolve_city_code(self, city: str) -> str:
        """解析并缓存高德路线规划 2.0 公交模式所需的 citycode。"""

        cache_key = city.strip().lower()
        cached = self._city_code_cache.get(cache_key)
        if cached:
            return cached
        raw = self.raw_client.district_search(city)
        payload = validate_amap_response(raw)
        city_code = normalize_city_code(payload)
        if not city_code:
            raise AmapProviderError(
                f"Amap returned no citycode for {city}",
                kind=AmapErrorKind.INVALID_OUTPUT,
                retryable=True,
            )
        self._city_code_cache[cache_key] = city_code
        return city_code

    @staticmethod
    def _unavailable_route(
        leg: RouteLegRequest,
        exc: AmapProviderError,
    ) -> RouteEstimate:
        return RouteEstimate(
            day_index=leg.day_index,
            leg_index=leg.leg_index,
            leg_type=leg.leg_type,
            date=leg.date,
            origin_name=leg.origin.name,
            destination_name=leg.destination.name,
            mode=leg.mode,
            available=False,
            error_code=f"AMAP_{exc.kind.value.upper()}",
            error_message=str(exc),
        )

    @staticmethod
    def _bind_cached_route(
        estimate: RouteEstimate,
        leg: RouteLegRequest,
    ) -> RouteEstimate:
        """复用缓存中的路线指标，同时恢复当前行程对应的分段元数据。"""

        return estimate.model_copy(
            update={
                "day_index": leg.day_index,
                "leg_index": leg.leg_index,
                "leg_type": leg.leg_type,
                "date": leg.date,
                "origin_name": leg.origin.name,
                "destination_name": leg.destination.name,
                "mode": leg.mode,
                "cache_hit": True,
            }
        )

    def _cache_get(
        self,
        cache_key: str,
        leg: RouteLegRequest,
    ) -> RouteEstimate | None:
        if self.route_cache is None:
            return None
        try:
            cached = self.route_cache.get(cache_key)
        except Exception:
            # 路线缓存只是性能优化，缓存故障不能阻断旅行规划主流程。
            logger.warning("Route cache read failed", exc_info=True)
            return None
        if cached is None:
            return None
        return self._bind_cached_route(cached, leg)

    def _cache_set(self, cache_key: str, estimate: RouteEstimate) -> None:
        if self.route_cache is None:
            return
        ttl_seconds = (
            settings.AMAP_ROUTE_CACHE_TTL_SECONDS
            if estimate.available
            else settings.AMAP_ROUTE_UNAVAILABLE_CACHE_TTL_SECONDS
        )
        if ttl_seconds <= 0:
            return
        try:
            self.route_cache.set(
                cache_key,
                estimate,
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            # 缓存写入失败不能把已经成功的高德请求变成工具失败。
            logger.warning("Route cache write failed", exc_info=True)

    def _prepare_leg(self, city: str, leg: RouteLegRequest) -> RouteLegRequest:
        """生成缓存键和 HTTP 请求前，补齐公交路线所需的城市编码。"""

        if leg.mode != "transit":
            return leg
        origin_city_code = leg.origin.city_code
        destination_city_code = leg.destination.city_code
        fallback_city_code = origin_city_code or destination_city_code
        if not fallback_city_code:
            fallback_city_code = self.resolve_city_code(city)
        return leg.model_copy(
            update={
                "origin": leg.origin.model_copy(
                    update={"city_code": origin_city_code or fallback_city_code}
                ),
                "destination": leg.destination.model_copy(
                    update={"city_code": destination_city_code or fallback_city_code}
                ),
            }
        )

    def estimate_routes(
        self,
        *,
        city: str,
        plan_fingerprint: str,
        legs: list[RouteLegRequest],
        limit: int | None = None,
    ) -> RouteEstimateResult:
        """逐段独立查询路线，避免单段失败导致整批结果全部丢失。"""

        maximum = (
            max(0, settings.AMAP_MAX_ROUTE_LEGS)
            if limit is None
            else max(0, limit)
        )
        selected = legs[:maximum]
        estimates: list[RouteEstimate] = []
        cache_hits = 0
        cache_misses = 0

        for original_leg in selected:
            cache_key: str | None = None
            try:
                leg = self._prepare_leg(city, original_leg)
                cache_key = route_leg_cache_key(leg)
                cached = self._cache_get(cache_key, leg)
                if cached is not None:
                    cache_hits += 1
                    estimates.append(cached)
                    continue
                if self.route_cache is not None:
                    cache_misses += 1

                raw = self.raw_client.route(
                    origin=leg.origin.location,
                    destination=leg.destination.location,
                    mode=leg.mode,
                    origin_poi_id=leg.origin.poi_id,
                    destination_poi_id=leg.destination.poi_id,
                    origin_city_code=leg.origin.city_code,
                    destination_city_code=leg.destination.city_code,
                )
                payload = validate_amap_response(raw)
                estimate = normalize_route(payload, leg=leg, mode=leg.mode)
            except AmapProviderError as exc:
                # 鉴权失败和限流会影响整个供应商请求，应交给统一执行策略
                # 快速失败或重试；已经缓存的成功分段不会被重复请求。
                if exc.kind in {AmapErrorKind.AUTHORIZATION, AmapErrorKind.RATE_LIMIT}:
                    raise
                estimate = self._unavailable_route(original_leg, exc)

            estimates.append(estimate)
            if cache_key is not None:
                self._cache_set(cache_key, estimate)

        return RouteEstimateResult(
            plan_fingerprint=plan_fingerprint,
            requested_legs=len(legs),
            evaluated_legs=len(selected),
            truncated_legs=max(0, len(legs) - len(selected)),
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            failed_legs=sum(not estimate.available for estimate in estimates),
            routes=estimates,
        )
