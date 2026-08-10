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


RouteMode = Literal["walking", "driving", "transit"]
RouteLegType = Literal[
    "hotel_departure",
    "between_attractions",
    "hotel_return",
]


class RoutePoint(BaseModel):
    """One route endpoint; POI ID and city code improve route accuracy."""

    name: str = Field(min_length=1)
    location: GeoPoint
    poi_id: str = ""
    city_code: str = ""


class RouteLegRequest(BaseModel):
    """One deterministic route leg between adjacent attractions on the same day."""

    day_index: int = Field(ge=0)
    leg_index: int = Field(ge=0)
    leg_type: RouteLegType = "between_attractions"
    date: str = ""
    origin: RoutePoint
    destination: RoutePoint
    mode: RouteMode


class RouteEstimate(BaseModel):
    """One normalized route returned by Amap Route Planning 2.0."""

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
    """Real route estimates for one plan, including provider-side cropping counts."""

    provider: Literal["amap"] = "amap"
    plan_fingerprint: str = Field(min_length=1)
    requested_legs: int = Field(default=0, ge=0)
    evaluated_legs: int = Field(default=0, ge=0)
    truncated_legs: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)
    failed_legs: int = Field(default=0, ge=0)
    routes: list[RouteEstimate] = Field(default_factory=list)
