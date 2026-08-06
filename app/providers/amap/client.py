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
    RouteEstimate,
    RouteEstimateResult,
    RouteLegRequest,
    RouteMode,
    WeatherSearchResult,
)
from app.providers.amap.route_cache import RouteCache, route_leg_cache_key
from app.providers.amap.normalizers import (
    normalize_attractions,
    normalize_city_code,
    normalize_hotels,
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
    def text_search(cls, *, keywords: str, city: str) -> dict[str, Any]:
        return cls._get_json(
            "https://restapi.amap.com/v3/place/text",
            {
                "key": settings.AMAP_API_KEY,
                "keywords": keywords,
                "city": city,
                "output": "json",
                "page_size": min(
                    25,
                    max(
                        settings.AMAP_MAX_ATTRACTION_CANDIDATES,
                        settings.AMAP_MAX_HOTEL_CANDIDATES,
                        10,
                    ),
                ),
            },
        )

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
        """Amap accepts longitude first and at most six decimal places."""

        return f"{point.longitude:.6f},{point.latitude:.6f}"

    @classmethod
    def district_search(cls, city: str) -> dict[str, Any]:
        """Resolve a city name to the citycode required by the transit API."""

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
        """Call Amap Route Planning 2.0 for one route leg."""

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
            # Amap requires transit POI IDs to be supplied as a pair.
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
    ):
        self.raw_client = raw_client
        self.route_cache = route_cache
        self._city_code_cache: dict[str, str] = {}

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
        """Resolve and cache the citycode needed by Route Planning 2.0 transit."""

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
        """Reuse cached metrics while restoring metadata from the current plan."""

        return estimate.model_copy(
            update={
                "day_index": leg.day_index,
                "leg_index": leg.leg_index,
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
            # Route caching is an optimization. A cache outage must not block planning.
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
            # Do not turn a successful provider call into a failed tool action.
            logger.warning("Route cache write failed", exc_info=True)

    def _prepare_leg(self, city: str, leg: RouteLegRequest) -> RouteLegRequest:
        """Fill transit city codes before building the cache key and HTTP request."""

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
        """Query each route independently so one bad leg does not discard the batch."""

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
                # Authorization and throttling affect the whole provider request. Let the
                # execution policy fail fast or retry; cached successful legs avoid repeats.
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
