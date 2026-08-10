"""高德地图 Provider：负责传输、错误适配和稳定输出模型。"""

from app.providers.amap.client import AmapClient, AmapProviderClient
from app.providers.amap.errors import AmapErrorKind, AmapProviderError
from app.providers.amap.route_cache import RouteCache, route_leg_cache_key
from app.providers.amap.models import (
    AttractionCandidate,
    AttractionSearchResult,
    GeoPoint,
    HotelCandidate,
    HotelSearchResult,
    NearbyAttractionSearchResult,
    RouteEstimate,
    RouteEstimateResult,
    RouteLegRequest,
    RouteMode,
    RoutePoint,
    WeatherForecast,
    WeatherSearchResult,
)

__all__ = [
    "AmapClient",
    "AmapErrorKind",
    "AmapProviderClient",
    "AmapProviderError",
    "AttractionCandidate",
    "AttractionSearchResult",
    "GeoPoint",
    "HotelCandidate",
    "HotelSearchResult",
    "NearbyAttractionSearchResult",
    "RouteCache",
    "RouteEstimate",
    "RouteEstimateResult",
    "RouteLegRequest",
    "RouteMode",
    "RoutePoint",
    "WeatherForecast",
    "WeatherSearchResult",
    "route_leg_cache_key",
]
