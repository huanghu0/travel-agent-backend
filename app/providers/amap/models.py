"""高德标准化输出模型。

这些模型是 Provider 与业务工具层之间的稳定契约。高德原始字段发生变化时，
只需要调整 normalizers，而不需要让 PlannerAgent、AgentState 或校验器理解供应商 JSON。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GeoPoint(BaseModel):
    """统一使用经度、纬度两个浮点数字段表示坐标。"""

    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class PlaceCandidate(BaseModel):
    """景点和酒店共享的紧凑 POI 字段。"""

    poi_id: str = ""
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    location: GeoPoint
    district: str = ""
    city_code: str = ""
    adcode: str = ""
    rating: float | None = Field(default=None, ge=0, le=5)
    telephone: str = ""


class AttractionCandidate(PlaceCandidate):
    """可供规划模型选择的景点候选。"""

    category: str = ""
    opening_hours: str = ""
    closed_dates: list[str] = Field(default_factory=list)


class HotelCandidate(PlaceCandidate):
    """可供规划模型选择的酒店候选。"""

    type: str = ""
    estimated_cost: float | None = Field(default=None, ge=0)


class WeatherForecast(BaseModel):
    """单日天气预报；字段名与 TripPlan.weather_info 尽量保持一致。"""

    date: str = Field(min_length=1)
    day_weather: str = ""
    night_weather: str = ""
    day_temp: float | None = None
    night_temp: float | None = None
    day_wind_direction: str = ""
    night_wind_direction: str = ""
    day_wind_power: str = ""
    night_wind_power: str = ""


class AttractionSearchResult(BaseModel):
    """景点查询的稳定、已裁剪输出。"""

    provider: Literal["amap"] = "amap"
    query_city: str
    keywords: str
    total_received: int = Field(default=0, ge=0)
    candidates: list[AttractionCandidate] = Field(default_factory=list)


class NearbyAttractionSearchResult(AttractionSearchResult):
    """一次有界周边搜索返回的标准化景点 POI。"""

    center: GeoPoint
    radius_meters: int = Field(ge=100, le=50000)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=25)


class HotelSearchResult(BaseModel):
    """酒店查询的稳定、已裁剪输出。"""

    provider: Literal["amap"] = "amap"
    query_city: str
    keywords: str = "酒店"
    total_received: int = Field(default=0, ge=0)
    candidates: list[HotelCandidate] = Field(default_factory=list)


class WeatherSearchResult(BaseModel):
    """天气查询的稳定、已裁剪输出。"""

    provider: Literal["amap"] = "amap"
    query_city: str
    city: str = ""
    province: str = ""
    report_time: str = ""
    forecasts: list[WeatherForecast] = Field(default_factory=list)


class PoiCandidate(PlaceCandidate):
    """通用 POI 候选，供地点解析、交通枢纽和应急设施等场景复用。"""

    category: str = ""
    type_code: str = ""
    opening_hours: str = ""
    average_cost: float | None = Field(default=None, ge=0)
    distance_meters: int | None = Field(default=None, ge=0)


class PoiSearchResult(BaseModel):
    """POI 搜索 2.0 的稳定裁剪结果。"""

    provider: Literal["amap"] = "amap"
    query_city: str
    keywords: str
    types: str = ""
    center: GeoPoint | None = None
    radius_meters: int | None = Field(default=None, ge=100, le=50000)
    total_received: int = Field(default=0, ge=0)
    candidates: list[PoiCandidate] = Field(default_factory=list)


class RestaurantSearchSnapshot(BaseModel):
    """与具体行程锚点无关、可跨会话复用的周边餐饮搜索快照。"""

    provider: Literal["amap"] = "amap"
    query_city: str
    keywords: str
    center: GeoPoint
    radius_meters: int = Field(ge=100, le=50000)
    page_size: int = Field(default=20, ge=1, le=25)
    total_received: int = Field(default=0, ge=0)
    candidates: list[PoiCandidate] = Field(default_factory=list)


class RestaurantCandidate(PlaceCandidate):
    """真实餐饮 POI，携带确定性餐次锚点和距离信息。"""

    category: str = ""
    type_code: str = ""
    opening_hours: str = ""
    average_cost: float | None = Field(default=None, ge=0)
    distance_meters: int | None = Field(default=None, ge=0)
    anchor_id: str = ""
    day_index: int = Field(default=0, ge=0)
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] = "lunch"


class RestaurantSearchAnchor(BaseModel):
    """一次附近餐饮搜索对应的行程餐次锚点。"""

    anchor_id: str = Field(min_length=1)
    day_index: int = Field(ge=0)
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    name: str = Field(min_length=1)
    location: GeoPoint


class RestaurantSearchResult(BaseModel):
    """多个餐次锚点的真实餐饮候选集合。"""

    provider: Literal["amap"] = "amap"
    query_city: str
    keywords: str
    requested_anchors: int = Field(default=0, ge=0)
    searched_anchors: int = Field(default=0, ge=0)
    truncated_anchors: int = Field(default=0, ge=0)
    total_received: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)
    candidates: list[RestaurantCandidate] = Field(default_factory=list)


class PoiDetailResult(BaseModel):
    """按高德 POI ID 查询的标准化详情。"""

    provider: Literal["amap"] = "amap"
    poi_id: str
    found: bool = False
    candidate: PoiCandidate | None = None


class LocationResolutionResult(BaseModel):
    """把用户地点文本解析成可用于路线查询的唯一坐标。"""

    provider: Literal["amap"] = "amap"
    query: str
    city: str = ""
    resolved: bool = False
    source: Literal["poi", "geocode", "none"] = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    candidate: PoiCandidate | None = None


RouteMode = Literal["walking", "driving", "transit"]
RouteLegType = Literal[
    "hotel_departure",
    "between_attractions",
    "hotel_return",
]


class RoutePoint(BaseModel):
    """一个路线端点；POI ID 和城市编码用于提高路线查询精度。"""

    name: str = Field(min_length=1)
    location: GeoPoint
    poi_id: str = ""
    city_code: str = ""


class RouteLegRequest(BaseModel):
    """同一天内酒店和相邻景点之间的一条确定性路线分段请求。"""

    day_index: int = Field(ge=0)
    leg_index: int = Field(ge=0)
    leg_type: RouteLegType = "between_attractions"
    date: str = ""
    origin: RoutePoint
    destination: RoutePoint
    mode: RouteMode


class RouteEstimate(BaseModel):
    """高德路线规划 2.0 返回的一条标准化路线结果。"""

    provider: Literal["amap"] = "amap"
    day_index: int = Field(ge=0)
    leg_index: int = Field(ge=0)
    leg_type: RouteLegType = "between_attractions"
    date: str = ""
    origin_name: str
    destination_name: str
    mode: RouteMode
    available: bool = True
    distance_meters: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    error_message: str | None = None
    cache_hit: bool = False


class RouteEstimateResult(BaseModel):
    """一个行程的真实路线结果，包含请求、评估和供应商裁剪数量。"""

    provider: Literal["amap"] = "amap"
    plan_fingerprint: str = Field(min_length=1)
    requested_legs: int = Field(default=0, ge=0)
    evaluated_legs: int = Field(default=0, ge=0)
    truncated_legs: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)
    failed_legs: int = Field(default=0, ge=0)
    routes: list[RouteEstimate] = Field(default_factory=list)
