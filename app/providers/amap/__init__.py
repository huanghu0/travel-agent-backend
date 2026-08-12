"""高德地图 Provider：负责传输、错误适配和稳定输出模型。"""

from app.providers.amap.client import AmapClient, AmapProviderClient
from app.providers.amap.errors import AmapErrorKind, AmapProviderError
from app.providers.amap.restaurant_cache import (
    RestaurantCache,
    restaurant_search_cache_key,
)
from app.providers.amap.route_cache import RouteCache, route_leg_cache_key
from app.providers.amap.models import (
    AttractionCandidate,
    AttractionSearchResult,
    GeoPoint,
    HotelCandidate,
    HotelSearchResult,
    NearbyAttractionSearchResult,
    PoiCandidate,
    RestaurantCandidate,
    RestaurantSearchAnchor,
    RestaurantSearchResult,
    RestaurantSearchSnapshot,
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
    "PoiCandidate",
    "RestaurantCache",
    "RestaurantCandidate",
    "RestaurantSearchAnchor",
    "RestaurantSearchResult",
    "RestaurantSearchSnapshot",
    "RouteCache",
    "RouteEstimate",
    "RouteEstimateResult",
    "RouteLegRequest",
    "RouteMode",
    "RoutePoint",
    "WeatherForecast",
    "WeatherSearchResult",
    "restaurant_search_cache_key",
    "route_leg_cache_key",
]
