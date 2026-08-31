"""旅行场景工具装配：定义输入契约，并把高德和规划能力加入 ToolRegistry。"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, Field

from app.providers.amap.client import AmapProviderClient
from app.providers.amap.errors import AmapProviderError, validate_amap_response
from app.providers.amap.restaurant_cache import RestaurantCache
from app.providers.amap.route_cache import RouteCache
from app.providers.amap.models import (
    AttractionSearchResult,
    GeoPoint,
    HotelSearchResult,
    LocationResolutionResult,
    NearbyAttractionSearchResult,
    PoiDetailResult,
    PoiSearchResult,
    RestaurantSearchAnchor,
    RestaurantSearchResult,
    RouteEstimate,
    RouteEstimateResult,
    RouteLegRequest,
    WeatherSearchResult,
)
from app.rag.interfaces import RagRetriever
from app.rag.models import RagContext
from app.rag.retrieval import NoOpRagRetriever
from app.schemas.trip_schema import TripPlan, TripRequest
from app.tools.models import ToolErrorType
from app.tools.registry import CallInjector, ToolDefinition, ToolRegistry, ToolResultError
from app.validation import TripValidationResult


class SearchAttractionsInput(BaseModel):
    city: str = Field(min_length=1)
    preferences: list[str] = Field(default_factory=list)


class SupplementAttractionsInput(BaseModel):
    city: str = Field(min_length=1)
    keywords: str = Field(min_length=1)
    center: GeoPoint
    radius_meters: int = Field(ge=100, le=50000)
    page: int = Field(default=1, ge=1, le=100)
    page_size: int = Field(default=20, ge=1, le=25)
    day_index: int = Field(ge=0)
    attraction_index: int = Field(ge=0)
    target_attraction_name: str = Field(min_length=1)
    anchor_names: list[str] = Field(default_factory=list)


class GetWeatherInput(BaseModel):
    city: str = Field(min_length=1)


class SearchHotelsInput(BaseModel):
    city: str = Field(min_length=1)


class SearchPoisInput(BaseModel):
    """通用地点搜索输入；有 center 时执行周边搜索。"""

    city: str = Field(default="")
    keywords: str = Field(min_length=1)
    types: str = Field(default="")
    center: GeoPoint | None = None
    radius_meters: int | None = Field(default=None, ge=100, le=50000)
    limit: int | None = Field(default=None, ge=1, le=25)


class SearchRestaurantsInput(BaseModel):
    """一次有界批量餐饮搜索输入。"""

    city: str = Field(min_length=1)
    keywords: str = Field(default="餐厅", min_length=1)
    anchors: list[RestaurantSearchAnchor] = Field(default_factory=list)
    radius_meters: int | None = Field(default=None, ge=100, le=50000)


class GetPoiDetailInput(BaseModel):
    poi_id: str = Field(min_length=1)


class ResolveLocationInput(BaseModel):
    query: str = Field(min_length=1)
    city: str = Field(default="")


class EstimateRoutesInput(BaseModel):
    city: str = Field(min_length=1)
    plan_fingerprint: str = Field(min_length=1)
    legs: list[RouteLegRequest] = Field(default_factory=list)


class GeneratePlanInput(BaseModel):
    session_id: str = Field(min_length=1)
    request: TripRequest
    attractions: dict[str, Any]
    weather: dict[str, Any]
    hotels: dict[str, Any]


class GeneratePlanResult(BaseModel):
    trip_plan: TripPlan
    rag_context: RagContext


class RepairPlanInput(BaseModel):
    request: TripRequest
    current_plan: TripPlan
    validation_result: TripValidationResult
    attractions: dict[str, Any]
    weather: dict[str, Any]
    hotels: dict[str, Any]


def _to_tool_error(exc: AmapProviderError) -> ToolResultError:
    """在 Provider 边界把高德错误转换为 ToolRegistry 的统一错误。"""

    return ToolResultError(
        str(exc),
        error_type=ToolErrorType(exc.kind.value),
        retryable=exc.retryable,
        provider_code=exc.provider_code,
        provider_message=exc.provider_message,
    )


def _call_amap(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except AmapProviderError as exc:
        raise _to_tool_error(exc) from exc


def validate_map_result(result: Any) -> dict[str, Any]:
    """兼容旧 Agent：校验其返回的高德原始业务状态。"""

    try:
        return validate_amap_response(result)
    except AmapProviderError as exc:
        raise _to_tool_error(exc) from exc


def _attraction_keywords(preferences: list[str]) -> str:
    keywords = [item.strip() for item in preferences if item and item.strip()]
    return ",".join(keywords) if keywords else "景点"


def _standardized_provider(
    map_provider: Any | None,
    route_cache: RouteCache | None,
    restaurant_cache: RestaurantCache | None,
) -> Any:
    """兼容注入原始高德客户端，也允许直接注入已标准化 Provider。"""

    if map_provider is None:
        return AmapProviderClient(
            route_cache=route_cache,
            restaurant_cache=restaurant_cache,
        )
    if all(
        callable(getattr(map_provider, name, None))
        for name in ("search_attractions", "search_hotels", "get_weather")
    ):
        return map_provider
    return AmapProviderClient(
        raw_client=map_provider,
        route_cache=route_cache,
        restaurant_cache=restaurant_cache,
    )


def build_trip_tool_registry(
    *,
    planner_agent: Any,
    map_provider: Any | None = None,
    route_cache: RouteCache | None = None,
    restaurant_cache: RestaurantCache | None = None,
    attraction_agent: Any | None = None,
    weather_agent: Any | None = None,
    hotel_agent: Any | None = None,
    call_injector: CallInjector | None = None,
    rag_retriever: RagRetriever | None = None,
) -> ToolRegistry:
    """构建固定工具白名单。

    生产环境的三个事实查询动作经过高德标准化层，只向状态和 LLM 写入去重、
    过滤、排序、裁剪后的稳定结构。可选旧 Agent 参数仅用于迁移兼容。
    """

    provider = _standardized_provider(map_provider, route_cache, restaurant_cache)
    retriever = (
        rag_retriever if rag_retriever is not None else NoOpRagRetriever()
    )

    # 步骤 1：选择事实查询处理器。默认走标准化 Provider；显式旧 Agent 保留原链路。
    if attraction_agent is None:
        def search_attractions(value: SearchAttractionsInput) -> AttractionSearchResult:
            keywords = _attraction_keywords(value.preferences)
            return _call_amap(
                lambda: provider.search_attractions(city=value.city, keywords=keywords)
            )

        attraction_output_model: type[BaseModel] | None = AttractionSearchResult
        attraction_validator = None
        attraction_llm_cost = 0
    else:
        search_attractions = lambda value: attraction_agent.search_attractions(
            value.city,
            value.preferences,
        )
        attraction_output_model = None
        attraction_validator = validate_map_result
        attraction_llm_cost = 1

    def supplement_attractions(
        value: SupplementAttractionsInput,
    ) -> NearbyAttractionSearchResult:
        searcher = getattr(provider, "search_nearby_attractions", None)
        if not callable(searcher):
            return NearbyAttractionSearchResult(
                query_city=value.city,
                keywords=value.keywords,
                candidates=[],
                center=value.center,
                radius_meters=value.radius_meters,
                page=value.page,
                page_size=value.page_size,
            )
        return _call_amap(
            lambda: searcher(
                city=value.city,
                keywords=value.keywords,
                center=value.center,
                radius_meters=value.radius_meters,
                page=value.page,
                page_size=value.page_size,
            )
        )

    if weather_agent is None:
        def get_weather(value: GetWeatherInput) -> WeatherSearchResult:
            return _call_amap(lambda: provider.get_weather(value.city))

        weather_output_model: type[BaseModel] | None = WeatherSearchResult
        weather_validator = None
        weather_llm_cost = 0
    else:
        get_weather = lambda value: weather_agent.get_city_weather(value.city)
        weather_output_model = None
        weather_validator = validate_map_result
        weather_llm_cost = 1

    if hotel_agent is None:
        def search_hotels(value: SearchHotelsInput) -> HotelSearchResult:
            return _call_amap(
                lambda: provider.search_hotels(city=value.city, keywords="酒店")
            )

        hotel_output_model: type[BaseModel] | None = HotelSearchResult
        hotel_validator = None
        hotel_llm_cost = 0
    else:
        search_hotels = lambda value: hotel_agent.search_hotels(value.city)
        hotel_output_model = None
        hotel_validator = validate_map_result
        hotel_llm_cost = 1

    # 步骤 2：装配通用地点、真实餐饮、详情与地址解析能力。
    def search_pois(value: SearchPoisInput) -> PoiSearchResult:
        searcher = getattr(provider, "search_pois", None)
        if not callable(searcher):
            return PoiSearchResult(
                query_city=value.city,
                keywords=value.keywords,
                types=value.types,
                center=value.center,
                radius_meters=value.radius_meters,
            )
        return _call_amap(
            lambda: searcher(
                city=value.city,
                keywords=value.keywords,
                types=value.types,
                center=value.center,
                radius_meters=value.radius_meters,
                limit=value.limit,
            )
        )

    def search_restaurants(value: SearchRestaurantsInput) -> RestaurantSearchResult:
        searcher = getattr(provider, "search_restaurants", None)
        if not callable(searcher) or not value.anchors:
            return RestaurantSearchResult(
                query_city=value.city,
                keywords=value.keywords,
                requested_anchors=len(value.anchors),
            )
        return _call_amap(
            lambda: searcher(
                city=value.city,
                keywords=value.keywords,
                anchors=value.anchors,
                radius_meters=value.radius_meters,
            )
        )

    def get_poi_detail(value: GetPoiDetailInput) -> PoiDetailResult:
        getter = getattr(provider, "get_poi_detail", None)
        if not callable(getter):
            return PoiDetailResult(poi_id=value.poi_id)
        return _call_amap(lambda: getter(value.poi_id))

    def resolve_location(value: ResolveLocationInput) -> LocationResolutionResult:
        resolver = getattr(provider, "resolve_location", None)
        if not callable(resolver):
            return LocationResolutionResult(query=value.query, city=value.city)
        return _call_amap(
            lambda: resolver(query=value.query, city=value.city)
        )

    # 步骤 3：定义真实路线查询处理器；短行程直接返回空结果，避免无效 HTTP。
    def estimate_routes(value: EstimateRoutesInput) -> RouteEstimateResult:
        # 没有可连接地点的短行程不需要发起外部路线请求。
        if not value.legs:
            return RouteEstimateResult(
                plan_fingerprint=value.plan_fingerprint,
                requested_legs=0,
                evaluated_legs=0,
                truncated_legs=0,
                routes=[],
            )
        estimator = getattr(provider, "estimate_routes", None)
        if callable(estimator):
            return _call_amap(
                lambda: estimator(
                    city=value.city,
                    plan_fingerprint=value.plan_fingerprint,
                    legs=value.legs,
                )
            )

        # 兼容没有实现路线查询能力的已注入标准化 Provider。
        routes = [
            RouteEstimate(
                day_index=leg.day_index,
                leg_index=leg.leg_index,
                leg_type=leg.leg_type,
                date=leg.date,
                origin_name=leg.origin.name,
                destination_name=leg.destination.name,
                mode=leg.mode,
                available=False,
                error_code="ROUTE_PROVIDER_UNAVAILABLE",
                error_message="Injected map provider does not implement route estimation",
            )
            for leg in value.legs
        ]
        return RouteEstimateResult(
            plan_fingerprint=value.plan_fingerprint,
            requested_legs=len(value.legs),
            evaluated_legs=len(value.legs),
            truncated_legs=0,
            routes=routes,
        )

    def generate_plan(value: GeneratePlanInput) -> GeneratePlanResult:
        rag_context = retriever.retrieve(
            value.request,
            exclude_session_id=value.session_id,
        )
        if rag_retriever is None:
            plan = planner_agent.generate_plan(
                value.request,
                value.attractions,
                value.weather,
                value.hotels,
            )
        else:
            plan = planner_agent.generate_plan(
                value.request,
                value.attractions,
                value.weather,
                value.hotels,
                rag_context=rag_context,
            )
        return GeneratePlanResult(
            trip_plan=TripPlan.model_validate(plan),
            rag_context=rag_context,
        )

    # 步骤 4：注册固定工具白名单和稳定输入/输出 Schema。
    # 验收测试可注入确定性故障；生产环境默认不传，工具调用路径不受影响。
    registry = ToolRegistry(call_injector=call_injector)
    registry.register(
        ToolDefinition(
            name="search_attractions",
            description="搜索并返回已过滤、去重、排序和裁剪的高德景点候选",
            input_model=SearchAttractionsInput,
            handler=search_attractions,
            output_model=attraction_output_model,
            invalid_output_retryable=True,
            result_validator=attraction_validator,
            llm_call_cost=attraction_llm_cost,
        )
    )
    registry.register(
        ToolDefinition(
            name="supplement_attractions",
            description="Search standardized Amap attraction candidates around commute anchors",
            input_model=SupplementAttractionsInput,
            handler=supplement_attractions,
            output_model=NearbyAttractionSearchResult,
            invalid_output_retryable=True,
            llm_call_cost=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_weather",
            description="查询并返回字段统一、按日期排序和裁剪的高德天气预报",
            input_model=GetWeatherInput,
            handler=get_weather,
            output_model=weather_output_model,
            invalid_output_retryable=True,
            result_validator=weather_validator,
            llm_call_cost=weather_llm_cost,
        )
    )
    registry.register(
        ToolDefinition(
            name="search_hotels",
            description="搜索并返回已过滤、去重、排序和裁剪的高德酒店候选",
            input_model=SearchHotelsInput,
            handler=search_hotels,
            output_model=hotel_output_model,
            invalid_output_retryable=True,
            result_validator=hotel_validator,
            llm_call_cost=hotel_llm_cost,
        )
    )
    registry.register(
        ToolDefinition(
            name="search_pois",
            description="搜索并返回标准化、去重和裁剪后的通用高德地点候选",
            input_model=SearchPoisInput,
            handler=search_pois,
            output_model=PoiSearchResult,
            invalid_output_retryable=True,
            llm_call_cost=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="search_restaurants",
            description="围绕行程锚点批量搜索真实高德餐厅候选",
            input_model=SearchRestaurantsInput,
            handler=search_restaurants,
            output_model=RestaurantSearchResult,
            invalid_output_retryable=True,
            llm_call_cost=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_poi_detail",
            description="按高德 POI ID 查询标准化地点详情",
            input_model=GetPoiDetailInput,
            handler=get_poi_detail,
            output_model=PoiDetailResult,
            invalid_output_retryable=True,
            llm_call_cost=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="resolve_location",
            description="把地点名称或地址解析为可用于路线查询的高德坐标",
            input_model=ResolveLocationInput,
            handler=resolve_location,
            output_model=LocationResolutionResult,
            invalid_output_retryable=True,
            llm_call_cost=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="estimate_routes",
            description="查询相邻景点间的高德真实路线距离和耗时",
            input_model=EstimateRoutesInput,
            handler=estimate_routes,
            output_model=RouteEstimateResult,
            invalid_output_retryable=True,
            llm_call_cost=0,
        )
    )
    registry.register(
        ToolDefinition(
            name="generate_plan",
            description="根据请求、景点、天气和酒店信息生成结构化旅行计划",
            input_model=GeneratePlanInput,
            handler=generate_plan,
            output_model=GeneratePlanResult,
            invalid_output_retryable=True,
            llm_call_cost=1,
        )
    )
    registry.register(
        ToolDefinition(
            name="repair_plan",
            description="根据确定性校验结果修复已有旅行计划",
            input_model=RepairPlanInput,
            handler=lambda value: planner_agent.repair_plan(
                value.request,
                value.current_plan,
                value.validation_result,
                value.attractions,
                value.weather,
                value.hotels,
            ),
            output_model=TripPlan,
            invalid_output_retryable=True,
            llm_call_cost=1,
        )
    )
    return registry
