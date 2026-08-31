from contextlib import asynccontextmanager
import asyncio
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from app.auth.dependencies import (
    build_current_user_dependency,
    build_optional_current_user_dependency,
)
from app.auth.models import User
from app.auth.router import build_auth_router
from app.auth.security import JwtCodec, PasswordSecurity
from app.auth.service import AuthService
from app.agent_runtime import (
    AgentActionError,
    AgentBudgetExceededError,
    AgentCheckpointError,
    AgentConvergenceError,
    AgentMaxStepsError,
    AgentState,
    AgentStatus,
    TripOrchestrator,
)
from app.agents import PlannerAgent
from app.core.config import settings
from app.core.llm import configure_llm_quota_controller
from app.evaluation import (
    AcceptanceScenario,
    FIXED_ACCEPTANCE_SCENARIOS,
    FixedAcceptanceBaselineReport,
)
from app.infrastructure.cache import CacheConfig, create_cache_store
from app.infrastructure.cache.read_models import ReadModelSnapshotCache
from app.infrastructure.redis import (
    RedisClientManager,
    RedisConfig,
    create_task_notification_bus,
)
from app.infrastructure.redis.rate_limit import (
    NoOpRateLimiter,
    ProviderQuotaController,
    QuotaPolicy,
    RedisRateLimiter,
)
from app.observability import RedisObservabilityConfig, RedisRuntimeObservability
from app.observability.rag_metrics import rag_metrics
from app.providers.amap.business_cache import AmapBusinessCache
from app.providers.amap.client import AmapClient, AmapProviderClient
from app.providers.amap.layered_cache import (
    LayeredRestaurantCache,
    LayeredRouteCache,
)
from app.memory import (
    AgentSessionSummary,
    ExecutionBaselineReport,
    QualityBaselineReport,
)
from app.persistence import (
    DraftConflictError,
    DraftNotFoundError,
    SessionNotFoundError,
    MySQLDatabaseConfig,
    TaskIdempotencyConflictError,
    TripTaskNotFoundError,
    VersionNotFoundError,
    create_persistence_stores,
)
from app.schemas.execution_view_schema import TripExecutionView
from app.schemas.trip_schema import TripPlanResponse, TripRequest
from app.schemas.trip_draft_schema import (
    ConfirmDraftResponse,
    DraftEvaluationResponse,
    TripDraft,
    TripDraftCreate,
    TripDraftUpdate,
    TripPlanVersion,
)
from app.rag.runtime import RagRuntime
from app.rag.text_builder import EmbeddingTextBuilder
from app.services import TripDraftService
from app.sharing.router import build_shared_guide_router
from app.sharing.service import SharedGuideService
from app.task_runtime import (
    TripPlanningTask,
    TripTaskCancelResponse,
    TripTaskCreateResponse,
)
from app.task_runtime.notifying_store import NotifyingTripTaskStore
from app.task_runtime.worker import TripTaskWorker, WorkerSettings
from app.tools import ToolDescriptor, build_trip_tool_registry
from app.tools.unsplash_tools import get_place_photo


# Redis 是可选加速层：初始化连接池管理器不会立即建立网络连接。
redis_client_manager = RedisClientManager(RedisConfig.from_settings(settings))
# 通用缓存始终暴露同一接口：启用 Redis 时使用 L1 缓存，否则使用 NoOp 自动绕过。
# 创建 Store 不会访问网络，Redis 故障也只会产生降级结果，不影响 MySQL 事实数据。
cache_store = create_cache_store(
    cache_config=CacheConfig.from_settings(settings),
    redis_client_manager=redis_client_manager,
)
# Redis Pub/Sub 仅发送可丢失唤醒；真正的任务、事件和取消状态仍写入数据库。
task_notification_bus = create_task_notification_bus(
    client_manager=redis_client_manager,
    enabled=settings.REDIS_TASK_NOTIFICATIONS_ENABLED,
    reconnect_delay_seconds=settings.REDIS_TASK_NOTIFICATION_RECONNECT_SECONDS,
)
# Redis Lua 限流在所有实例间共享计数；Redis 关闭时使用 NoOp，故障时按配置 fail-open。
rate_limiter = (
    RedisRateLimiter(
        redis_client_manager,
        key_builder=redis_client_manager.key_builder,
        fail_open=settings.REDIS_PROVIDER_RATE_LIMIT_FAIL_OPEN,
    )
    if settings.REDIS_ENABLED and settings.REDIS_PROVIDER_RATE_LIMIT_ENABLED
    else NoOpRateLimiter()
)
provider_quota_controller = ProviderQuotaController(
    rate_limiter,
    amap_policies=(
        QuotaPolicy("per-second", settings.AMAP_RATE_LIMIT_REQUESTS_PER_SECOND, 1),
        QuotaPolicy("per-minute", settings.AMAP_QUOTA_REQUESTS_PER_MINUTE, 60),
        QuotaPolicy("per-day", settings.AMAP_QUOTA_REQUESTS_PER_DAY, 86400),
    ),
    llm_policies=(
        QuotaPolicy("per-minute", settings.LLM_QUOTA_REQUESTS_PER_MINUTE, 60),
        QuotaPolicy("per-day", settings.LLM_QUOTA_REQUESTS_PER_DAY, 86400),
    ),
)
configure_llm_quota_controller(provider_quota_controller)

# execution-view 和任务进度只缓存前端读取快照，写入失败不影响事实数据。
read_model_cache = ReadModelSnapshotCache(
    cache_store,
    redis_client_manager.key_builder,
    execution_view_ttl_seconds=settings.EXECUTION_VIEW_CACHE_TTL_SECONDS,
    task_active_ttl_seconds=settings.TASK_PROGRESS_CACHE_ACTIVE_TTL_SECONDS,
    task_terminal_ttl_seconds=settings.TASK_PROGRESS_CACHE_TERMINAL_TTL_SECONDS,
)

@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用启动时恢复持久化任务队列，关闭时停止领取新任务。"""

    settings.validate_auth_runtime()
    # 先启动订阅器，再启动 Worker，避免服务启动窗口内错过新任务通知。
    task_notification_bus.start()
    redis_observability.start()
    if settings.TRIP_TASK_WORKER_ENABLED:
        trip_task_worker.start()
    rag_runtime.start()
    try:
        yield
    finally:
        rag_runtime.stop()
        if settings.TRIP_TASK_WORKER_ENABLED:
            trip_task_worker.stop()
        task_notification_bus.stop()
        redis_observability.stop()
        # Redis 属于可选加速层，关闭失败不能阻塞应用退出。
        redis_client_manager.close()


# 初始化FastAPI应用
app = FastAPI(
    lifespan=lifespan,
    title="旅行助手智能体API",
    description="基于FastAPI+LangChain的智能旅行规划助手",
    version="1.0.0",
    prefix="/api",
)

# 跨域配置（支持前端调用）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 初始化规划智能体、持久化 Store 和确定性编排器（单例）。
# 步骤 1：统一工厂根据 DATABASE_BACKEND 创建 Store；业务层不再直接依赖 SQLite。
persistence_stores = create_persistence_stores(
    backend=settings.DATABASE_BACKEND,
    sqlite_database_path=settings.AGENT_MEMORY_DB_PATH,
    mysql_config=MySQLDatabaseConfig.from_settings(settings),
    route_cache_enabled=settings.AMAP_ROUTE_CACHE_ENABLED,
    restaurant_cache_enabled=settings.AMAP_RESTAURANT_CACHE_ENABLED,
)
auth_service: AuthService | None = None
if persistence_stores.user_store is not None and settings.JWT_SECRET_KEY:
    try:
        auth_service = AuthService(
            user_store=persistence_stores.user_store,
            password_security=PasswordSecurity(),
            jwt_codec=JwtCodec(
                secret_key=settings.JWT_SECRET_KEY,
                algorithm=settings.JWT_ALGORITHM,
                expire_days=settings.JWT_EXPIRE_DAYS,
            ),
        )
    except ValueError:
        # lifespan 会给出统一且不包含密钥内容的配置错误。
        auth_service = None
current_user_dependency = build_current_user_dependency(auth_service)
optional_current_user_dependency = build_optional_current_user_dependency(auth_service)
app.include_router(build_auth_router(auth_service, current_user_dependency))
agent_state_store = persistence_stores.agent_state_store
trip_version_store = persistence_stores.trip_version_store
route_cache_l2 = persistence_stores.route_cache
restaurant_cache_l2 = persistence_stores.restaurant_cache
shared_guide_store = persistence_stores.shared_guide_store
# 步骤 2：RAG 与分享索引共享可选 DashScope/Qdrant 适配器；失败时只降级该边界。
rag_runtime = RagRuntime.from_settings(
    settings=settings,
    shared_store=shared_guide_store,
)
# 步骤 3：为高德路线和餐饮建立 Redis L1 → 数据库 L2 分层缓存。
# Redis 关闭或故障时由 NoOp/降级结果自动绕过，MySQL/SQLite L2 仍可继续工作。
route_cache = (
    LayeredRouteCache(
        l1_cache=cache_store,
        l2_cache=route_cache_l2,
        l1_key_builder=lambda cache_key: redis_client_manager.key_builder.literal(
            "cache", "route", cache_key
        ),
    )
    if route_cache_l2 is not None
    else None
)
restaurant_cache = (
    LayeredRestaurantCache(
        l1_cache=cache_store,
        l2_cache=restaurant_cache_l2,
        l1_key_builder=lambda cache_key: redis_client_manager.key_builder.literal(
            "cache", "restaurant", cache_key
        ),
    )
    if restaurant_cache_l2 is not None
    else None
)
# 步骤 4：标准化高德业务结果使用 Redis-only 热缓存，路线/餐饮继续使用两级缓存。
amap_business_cache = AmapBusinessCache(
    cache_store,
    redis_client_manager.key_builder,
)


class _QuotaAwareAmapClient(AmapClient):
    """仅生产装配使用，避免全局类状态影响测试或显式注入的 Provider。"""

    quota_controller = provider_quota_controller


amap_provider = AmapProviderClient(
    raw_client=_QuotaAwareAmapClient,
    route_cache=route_cache,
    restaurant_cache=restaurant_cache,
    business_cache=amap_business_cache,
)
# 步骤 5：注册工具白名单。景点、天气、酒店直接调用高德，不经过 LLM。
planner_agent = PlannerAgent()
trip_tool_registry = build_trip_tool_registry(
    planner_agent=planner_agent,
    map_provider=amap_provider,
    route_cache=route_cache,
    restaurant_cache=restaurant_cache,
    rag_retriever=rag_runtime.retriever,
)
# 步骤 6：编排器负责按固定状态机循环执行，并统一应用预算、重试和熔断策略。
trip_orchestrator = TripOrchestrator(
    tool_registry=trip_tool_registry,
    max_steps=settings.AGENT_MAX_STEPS,
    max_attempts_per_action=settings.AGENT_MAX_ATTEMPTS_PER_ACTION,
    max_repeated_action_inputs=settings.AGENT_MAX_REPEATED_ACTION_INPUTS,
    max_no_progress_steps=settings.AGENT_MAX_NO_PROGRESS_STEPS,
    max_local_actions_per_step=settings.AGENT_MAX_LOCAL_ACTIONS_PER_STEP,
    max_repair_attempts=settings.AGENT_MAX_REPAIR_ATTEMPTS,
    partial_acceptance_enabled=settings.AGENT_PARTIAL_ACCEPTANCE_ENABLED,
    partial_acceptance_min_score=settings.AGENT_PARTIAL_ACCEPTANCE_MIN_SCORE,
    partial_acceptance_max_validation_errors=(
        settings.AGENT_PARTIAL_ACCEPTANCE_MAX_VALIDATION_ERRORS
    ),
    partial_acceptance_max_schedule_overtime_minutes=(
        settings.AGENT_PARTIAL_ACCEPTANCE_MAX_SCHEDULE_OVERTIME_MINUTES
    ),
    partial_acceptance_max_unavailable_route_legs=(
        settings.AGENT_PARTIAL_ACCEPTANCE_MAX_UNAVAILABLE_ROUTE_LEGS
    ),
    partial_acceptance_max_excessive_commute_segments=(
        settings.AGENT_PARTIAL_ACCEPTANCE_MAX_EXCESSIVE_COMMUTE_SEGMENTS
    ),
    partial_acceptance_max_constraint_errors=(
        settings.AGENT_PARTIAL_ACCEPTANCE_MAX_CONSTRAINT_ERRORS
    ),
    partial_acceptance_min_attractions_per_day=(
        settings.AGENT_PARTIAL_ACCEPTANCE_MIN_ATTRACTIONS_PER_DAY
    ),
    partial_acceptance_allowed_error_codes=(
        settings.AGENT_PARTIAL_ACCEPTANCE_ALLOWED_ERROR_CODES
    ),
    max_route_optimization_attempts=(
        settings.AGENT_MAX_ROUTE_OPTIMIZATION_ATTEMPTS
    ),
    route_optimization_max_candidates=(
        settings.ROUTE_OPTIMIZATION_MAX_CANDIDATES
    ),
    route_optimization_min_improvement_percent=(
        settings.ROUTE_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT
    ),
    max_schedule_optimization_attempts=(
        settings.AGENT_MAX_SCHEDULE_OPTIMIZATION_ATTEMPTS
    ),
    schedule_optimization_max_candidates=(
        settings.SCHEDULE_OPTIMIZATION_MAX_CANDIDATES
    ),
    schedule_optimization_min_improvement_percent=(
        settings.SCHEDULE_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT
    ),
    schedule_default_start_time=settings.SCHEDULE_DEFAULT_START_TIME,
    schedule_default_end_time=settings.SCHEDULE_DEFAULT_END_TIME,
    schedule_lunch_duration_minutes=settings.SCHEDULE_LUNCH_DURATION_MINUTES,
    schedule_lunch_window_start=settings.CONSTRAINT_LUNCH_WINDOW_START,
    schedule_lunch_window_end=settings.CONSTRAINT_LUNCH_WINDOW_END,
    schedule_route_buffer_minutes=settings.SCHEDULE_ROUTE_BUFFER_MINUTES,
    schedule_attraction_buffer_minutes=(
        settings.SCHEDULE_ATTRACTION_BUFFER_MINUTES
    ),
    max_constraint_optimization_attempts=(
        settings.AGENT_MAX_CONSTRAINT_OPTIMIZATION_ATTEMPTS
    ),
    constraint_optimization_max_candidates=(
        settings.CONSTRAINT_OPTIMIZATION_MAX_CANDIDATES
    ),
    constraint_optimization_min_improvement_percent=(
        settings.CONSTRAINT_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT
    ),
    constraint_lunch_window_start=settings.CONSTRAINT_LUNCH_WINDOW_START,
    constraint_lunch_window_end=settings.CONSTRAINT_LUNCH_WINDOW_END,
    constraint_daily_attraction_soft_limit=(
        settings.CONSTRAINT_DAILY_ATTRACTION_SOFT_LIMIT
    ),
    max_commute_replacement_attempts=(
        settings.AGENT_MAX_COMMUTE_REPLACEMENT_ATTEMPTS
    ),
    commute_replacement_max_candidates=(
        settings.COMMUTE_REPLACEMENT_MAX_CANDIDATES
    ),
    max_commute_supplement_searches=(
        settings.AGENT_MAX_COMMUTE_SUPPLEMENT_SEARCHES
    ),
    commute_supplement_initial_radius_meters=(
        settings.COMMUTE_SUPPLEMENT_INITIAL_RADIUS_METERS
    ),
    commute_supplement_max_radius_meters=(
        settings.COMMUTE_SUPPLEMENT_MAX_RADIUS_METERS
    ),
    commute_supplement_page_size=settings.COMMUTE_SUPPLEMENT_PAGE_SIZE,
    commute_supplement_pool_max_candidates=(
        settings.COMMUTE_SUPPLEMENT_POOL_MAX_CANDIDATES
    ),
    commute_max_walking_minutes=settings.COMMUTE_MAX_WALKING_MINUTES,
    commute_max_transit_minutes=settings.COMMUTE_MAX_TRANSIT_MINUTES,
    commute_max_driving_minutes=settings.COMMUTE_MAX_DRIVING_MINUTES,
    minimum_total_attractions=settings.AGENT_MIN_TOTAL_ATTRACTIONS,
    max_content_refill_attempts=settings.AGENT_MAX_CONTENT_REFILL_ATTEMPTS,
    content_refill_max_candidates=settings.CONTENT_REFILL_MAX_CANDIDATES,
    content_refill_default_visit_duration_minutes=(
        settings.CONTENT_REFILL_DEFAULT_VISIT_DURATION_MINUTES
    ),
    max_duration_seconds=settings.AGENT_MAX_DURATION_SECONDS,
    max_tool_calls=settings.AGENT_MAX_TOOL_CALLS,
    max_llm_calls=settings.AGENT_MAX_LLM_CALLS,
    retry_base_delay_seconds=settings.AGENT_RETRY_BASE_DELAY_SECONDS,
    retry_max_delay_seconds=settings.AGENT_RETRY_MAX_DELAY_SECONDS,
    retry_jitter_seconds=settings.AGENT_RETRY_JITTER_SECONDS,
    circuit_failure_threshold=settings.AGENT_CIRCUIT_FAILURE_THRESHOLD,
    circuit_recovery_timeout_seconds=settings.AGENT_CIRCUIT_RECOVERY_TIMEOUT_SECONDS,
    state_store=agent_state_store,
    checkpoint_max_attempts=settings.AGENT_CHECKPOINT_MAX_ATTEMPTS,
    checkpoint_retry_base_delay_seconds=(
        settings.AGENT_CHECKPOINT_RETRY_BASE_DELAY_SECONDS
    ),
    checkpoint_retry_max_delay_seconds=(
        settings.AGENT_CHECKPOINT_RETRY_MAX_DELAY_SECONDS
    ),
)
# 草稿服务复用同一套工具注册表和确定性评估器，不额外创建 LLM 客户端。
trip_draft_service = TripDraftService(
    state_store=agent_state_store,
    version_store=trip_version_store,
    tool_registry=trip_tool_registry,
    orchestrator=trip_orchestrator,
)

# 步骤 7：只有 MySQL 事实 Store 存在时才暴露分享路由。关闭功能或适配器
# 降级时仍保留公开读取，所有写操作由服务边界统一返回 503。
shared_guide_service: SharedGuideService | None = None
if shared_guide_store is not None and settings.DATABASE_BACKEND == "mysql":
    share_max_limit = settings.SHARE_LIST_MAX_LIMIT
    if (
        not isinstance(share_max_limit, int)
        or isinstance(share_max_limit, bool)
        or share_max_limit < 1
    ):
        share_max_limit = 50
    share_default_limit = settings.SHARE_LIST_DEFAULT_LIMIT
    if (
        not isinstance(share_default_limit, int)
        or isinstance(share_default_limit, bool)
        or share_default_limit < 1
        or share_default_limit > share_max_limit
    ):
        share_default_limit = min(20, share_max_limit)
    shared_guide_service = SharedGuideService(
        state_store=agent_state_store,
        trip_draft_service=trip_draft_service,
        store=shared_guide_store,
        text_builder=EmbeddingTextBuilder(),
        embedding_client=rag_runtime.embedding_client,
        vector_index=rag_runtime.vector_index,
        write_enabled=bool(settings.SHARE_SQUARE_ENABLED and rag_runtime.ready),
        lease_seconds=settings.SHARE_INDEX_LEASE_SECONDS,
        max_attempts=settings.SHARE_INDEX_MAX_ATTEMPTS,
        retry_base_seconds=settings.SHARE_INDEX_RETRY_BASE_SECONDS,
        retry_max_seconds=settings.SHARE_INDEX_RETRY_MAX_SECONDS,
        metrics=rag_metrics,
    )
    app.include_router(
        build_shared_guide_router(
            shared_guide_service,
            current_user_dependency,
            optional_current_user_dependency,
            default_list_limit=share_default_limit,
            max_list_limit=share_max_limit,
        )
    )


# 阶段五：任务元数据与 SSE 事件使用同一持久化后端，独立于 AgentState 检查点。
# 通知装饰器只在数据库事务提交成功后发布 Redis 消息，发布失败不会覆盖写库结果。
trip_task_store = NotifyingTripTaskStore(
    delegate=persistence_stores.trip_task_store,
    notification_bus=task_notification_bus,
    snapshot_cache=read_model_cache,
)
trip_task_worker = TripTaskWorker(
    task_store=trip_task_store,
    state_store=agent_state_store,
    orchestrator=trip_orchestrator,
    notification_bus=task_notification_bus,
    settings=WorkerSettings(
        poll_interval_seconds=(
            settings.TRIP_TASK_NOTIFICATION_WORKER_FALLBACK_POLL_SECONDS
            if task_notification_bus.enabled
            else settings.TRIP_TASK_WORKER_POLL_SECONDS
        ),
        lease_seconds=settings.TRIP_TASK_LEASE_SECONDS,
        heartbeat_interval_seconds=settings.TRIP_TASK_HEARTBEAT_SECONDS,
        shutdown_timeout_seconds=settings.TRIP_TASK_SHUTDOWN_TIMEOUT_SECONDS,
    ),
)

# Redis 生产可观测性统一读取现有累计指标，不记录 Key、任务内容或用户输入。
redis_observability = RedisRuntimeObservability(
    config=RedisObservabilityConfig.from_settings(settings),
    client_manager=redis_client_manager,
    notification_bus=task_notification_bus,
    cache_store=cache_store,
    route_cache=route_cache,
    restaurant_cache=restaurant_cache,
    business_cache=amap_business_cache,
    rate_limiter=rate_limiter,
    worker=trip_task_worker,
    worker_fallback_poll_seconds=(
        settings.TRIP_TASK_NOTIFICATION_WORKER_FALLBACK_POLL_SECONDS
    ),
    sse_fallback_poll_seconds=(
        settings.TRIP_TASK_NOTIFICATION_SSE_FALLBACK_POLL_SECONDS
    ),
)
redis_observability.registry.register(rag_metrics)


_STAGE_NAMES = {
    "search_attractions": "景点搜索",
    "get_weather": "天气查询",
    "search_hotels": "酒店搜索",
    "generate_plan": "行程生成",
    "estimate_routes": "路线查询",
    "evaluate_commute": "\u5355\u6bb5\u901a\u52e4\u8bc4\u4f30",
    "replace_remote_attraction": "\u8fc7\u8fdc\u666f\u70b9\u66ff\u6362",
    "supplement_attractions": "\u9ad8\u5fb7\u5468\u8fb9\u5019\u9009\u8865\u5145",
    "optimize_routes": "路线优化",
    "evaluate_schedule": "时间轴评估",
    "optimize_schedule": "日程优化",
    "evaluate_constraints": "\u53ef\u6267\u884c\u6027\u7ea6\u675f\u8bc4\u4f30",
    "optimize_constraints": "\u7ea6\u675f\u51b2\u7a81\u4f18\u5316",
    "refill_attractions": "\u6700\u4f4e\u666f\u70b9\u4fdd\u969c",
    "rebuild_plan_content": "\u884c\u7a0b\u5185\u5bb9\u4e00\u81f4\u6027\u91cd\u5efa",
    "validate_plan": "行程校验",
    "repair_plan": "行程修复",
    "finish": "完成",
}


def _action_error_detail(exc: AgentActionError) -> str:
    stage = _STAGE_NAMES.get(exc.action.value, exc.action.value)
    return (
        f"旅行规划失败（阶段：{stage}，"
        f"步骤：{exc.state.current_step}/{exc.state.max_steps}，"
        f"尝试：{exc.attempt}，会话：{exc.state.session_id}）: {exc}"
    )


def _convergence_error_detail(exc: AgentConvergenceError) -> str:
    return (
        f"旅行规划失败（阶段：执行循环收敛，"
        f"步骤：{exc.state.current_step}/{exc.state.max_steps}，"
        f"会话：{exc.state.session_id}）: {exc.reason}"
    )


def _max_steps_error_detail(exc: AgentMaxStepsError) -> str:
    return (
        f"旅行规划失败（阶段：执行循环，"
        f"步骤：{exc.state.current_step}/{exc.state.max_steps}，"
        f"会话：{exc.state.session_id}）: {exc}"
    )


# ==================== 路由接口 ====================
@app.get("/api/poi/photo", summary="查询景点图片")
def get_poi_photo(
    name: str,
    _current_user: User = Depends(current_user_dependency),
):
    """根据景点名称获取Unsplash图片。"""
    return get_place_photo(name)


@app.get(
    "/api/agent/tools",
    summary="查询智能体可用工具白名单",
    response_model=list[ToolDescriptor],
)
def list_agent_tools(
    _current_user: User = Depends(current_user_dependency),
):
    """返回安全工具元数据，不包含处理器实现和密钥。"""

    return trip_tool_registry.describe()


@app.post(
    "/api/trip/tasks",
    summary="创建异步旅行规划任务",
    response_model=TripTaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_trip_task(
    request: TripRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current_user: User = Depends(current_user_dependency),
):
    """只持久化任务并唤醒 Worker，不在请求线程执行高德或 LLM。"""

    try:
        task, reused = trip_task_store.create_task(
            request,
            idempotency_key=idempotency_key,
            user_id=current_user.user_id,
        )
        trip_task_worker.wake()
        return TripTaskCreateResponse(
            task_id=task.task_id,
            session_id=task.session_id,
            status=task.status,
            created_at=task.created_at,
            reused=reused,
        )
    except TaskIdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get(
    "/api/trip/tasks/{task_id}",
    summary="查询异步旅行规划任务",
    response_model=TripPlanningTask,
)
def get_trip_task(
    task_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """页面刷新或断线恢复时，以该持久化快照为准。"""

    try:
        return trip_task_store.get_task(task_id, user_id=current_user.user_id)
    except TripTaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/trip/tasks/{task_id}/cancel",
    summary="取消等待中或执行中的旅行规划任务",
    response_model=TripTaskCancelResponse,
)
def cancel_trip_task(
    task_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """设置持久化取消标记；执行中的 Worker 会在下一安全检查点停止。"""

    try:
        task = trip_task_store.request_cancel(task_id, user_id=current_user.user_id)
        trip_task_worker.wake()
        return TripTaskCancelResponse(
            task_id=task.task_id,
            status=task.status,
            cancel_requested=task.cancel_requested,
            message=task.message,
        )
    except TripTaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/trip/tasks/{task_id}/events",
    summary="订阅异步旅行规划任务事件",
)
async def stream_trip_task_events(
    task_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after_event_id: int = Query(default=0, ge=0),
    current_user: User = Depends(current_user_dependency),
):
    """按 SQLite 自增 event_id 回放事件，SSE 重连时不会重复发送旧事件。"""

    try:
        trip_task_store.get_task(task_id, user_id=current_user.user_id)
    except TripTaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        header_cursor = int(last_event_id) if last_event_id else 0
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是整数") from exc
    initial_cursor = max(after_event_id, header_cursor, 0)

    async def event_generator():
        cursor = initial_cursor
        last_heartbeat = asyncio.get_running_loop().time()
        while True:
            if await request.is_disconnected():
                return
            # 查询数据库前保存通知游标，避免事件恰好写入时进入无谓等待。
            notification_cursor = task_notification_bus.task_cursor(task_id)
            events = await run_in_threadpool(
                trip_task_store.list_events,
                task_id,
                after_event_id=cursor,
                limit=200,
            )
            for event in events:
                cursor = event.event_id
                payload = json.dumps(
                    event.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield (
                    f"id: {event.event_id}\n"
                    f"event: {event.event_type}\n"
                    f"data: {payload}\n\n"
                )
            task = await run_in_threadpool(
                trip_task_store.get_task,
                task_id,
                user_id=current_user.user_id,
            )
            if task.terminal:
                return
            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= settings.TRIP_TASK_SSE_HEARTBEAT_SECONDS:
                yield ": heartbeat\n\n"
                last_heartbeat = now

            # Redis 消息只负责提前结束等待；醒来后仍从数据库按 event_id 回放，
            # 因而重复消息不会重复展示，断线重连仍沿用 Last-Event-ID。
            fallback_poll_seconds = (
                settings.TRIP_TASK_NOTIFICATION_SSE_FALLBACK_POLL_SECONDS
                if task_notification_bus.enabled
                else settings.TRIP_TASK_SSE_POLL_SECONDS
            )
            heartbeat_remaining = max(
                0.05,
                settings.TRIP_TASK_SSE_HEARTBEAT_SECONDS
                - (asyncio.get_running_loop().time() - last_heartbeat),
            )
            await run_in_threadpool(
                task_notification_bus.wait_for_task,
                task_id,
                notification_cursor,
                min(max(0.05, fallback_poll_seconds), heartbeat_remaining),
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/trip/plan", summary="生成旅行计划", response_model=TripPlanResponse)
async def generate_trip_plan(
    request: TripRequest,
    current_user: User = Depends(current_user_dependency),
):
    """
    使用确定性、有界循环生成旅行计划，并在每一步写入SQLite检查点。
    """
    try:
        # 步骤 1：在线程池中运行同步编排循环，避免高德和 LLM 的阻塞请求卡住 FastAPI 事件循环。
        state = await run_in_threadpool(
            trip_orchestrator.run,
            request,
            user_id=current_user.user_id,
        )

        # 步骤 2：编排器完成后，把最终行程、会话 ID 和实际执行步数返回给前端。
        return TripPlanResponse(
            success=True,
            message=(
                "旅行计划生成成功，部分非关键问题已保留为警告"
                if state.completion_mode == "partial"
                else "旅行计划生成成功"
            ),
            data=state.trip_plan,
            session_id=state.session_id,
            execution_steps=state.current_step,
            completion_mode=state.completion_mode,
            quality_level=(
                state.acceptance_report.quality_level.value
                if state.acceptance_report is not None
                else None
            ),
            quality_score=(
                state.acceptance_report.quality_score
                if state.acceptance_report is not None
                else None
            ),
            warnings=state.completion_warnings,
        )
    # 步骤 3：把运行时异常转换成前端容易定位的 HTTP 错误。
    except AgentCheckpointError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentBudgetExceededError as exc:
        # 时间、工具调用或 LLM 调用预算耗尽，属于暂时不可用。
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentActionError as exc:
        raise HTTPException(status_code=500, detail=_action_error_detail(exc)) from exc
    except AgentConvergenceError as exc:
        raise HTTPException(
            status_code=409, detail=_convergence_error_detail(exc)
        ) from exc
    except AgentMaxStepsError as exc:
        raise HTTPException(status_code=500, detail=_max_steps_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"旅行规划失败（阶段：智能体执行）: {exc}",
        ) from exc


@app.get(
    "/api/trip/analytics/execution-baseline",
    summary="查询状态跳转路径和完成率基线",
    response_model=ExecutionBaselineReport,
)
def get_execution_baseline(
    limit: int = Query(default=1000, ge=1, le=5000),
    status: AgentStatus | None = Query(default=None),
    city: str | None = Query(default=None, min_length=1, max_length=100),
    top_n: int = Query(default=20, ge=1, le=100),
    max_cycle_span: int = Query(default=12, ge=1, le=50),
    current_user: User = Depends(current_user_dependency),
):
    """从 SQLite 最近会话统计完成率、动作次数、跳转路径和常见循环。"""

    # 该接口只读取历史检查点，不执行高德、LLM 或任何行程修复动作。
    return agent_state_store.get_execution_baseline(
        limit=limit,
        status=status,
        city=city,
        top_n=top_n,
        max_cycle_span=max_cycle_span,
        user_id=current_user.user_id,
    )


@app.get(
    "/api/agent/analytics/quality-baseline",
    summary="查询部分完成与执行质量基线",
    response_model=QualityBaselineReport,
)
def get_quality_baseline(
    limit: int = Query(default=1000, ge=1, le=5000),
    status: AgentStatus | None = Query(default=None),
    city: str | None = Query(default=None, min_length=1, max_length=100),
    travel_days: int | None = Query(default=None, ge=1, le=30),
    transportation: str | None = Query(default=None, min_length=1, max_length=100),
    completion_mode: Literal["full", "partial"] | None = Query(default=None),
    quality_level: Literal[
        "excellent", "acceptable", "degraded", "unusable"
    ]
    | None = Query(default=None),
    top_n: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(current_user_dependency),
):
    """统计完整/部分完成率、质量分、警告代码以及工具和 LLM 消耗。"""

    # 只读取 SQLite 检查点；该接口不会触发新的旅行规划或外部 API 调用。
    return agent_state_store.get_quality_baseline(
        limit=limit,
        status=status,
        city=city,
        travel_days=travel_days,
        transportation=transportation,
        completion_mode=completion_mode,
        quality_level=quality_level,
        top_n=top_n,
        user_id=current_user.user_id,
    )


@app.get(
    "/api/agent/analytics/fixed-acceptance-scenarios",
    summary="查询固定端到端验收场景",
    response_model=list[AcceptanceScenario],
)
def list_fixed_acceptance_scenarios(
    _current_user: User = Depends(current_user_dependency),
):
    """返回五城市、1/3/5 日和三类交通方式的固定请求清单。"""

    return FIXED_ACCEPTANCE_SCENARIOS


@app.get(
    "/api/agent/analytics/fixed-acceptance-baseline",
    summary="查询固定端到端验收覆盖率和通过率",
    response_model=FixedAcceptanceBaselineReport,
)
def get_fixed_acceptance_baseline(
    limit: int = Query(default=5000, ge=1, le=10000),
    current_user: User = Depends(current_user_dependency),
):
    """用每个固定场景最近一次匹配会话执行离线确定性验收。"""

    return agent_state_store.get_fixed_acceptance_baseline(
        limit=limit,
        user_id=current_user.user_id,
    )


@app.get(
    "/api/trip/sessions",
    summary="查询旅行规划会话列表",
    response_model=list[AgentSessionSummary],
)
def list_trip_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    status: AgentStatus | None = Query(default=None),
    current_user: User = Depends(current_user_dependency),
):
    """按最近更新时间倒序查询会话摘要。"""

    return agent_state_store.list_sessions(
        limit=limit,
        status=status,
        user_id=current_user.user_id,
    )


@app.get(
    "/api/trip/sessions/{session_id}",
    summary="查询旅行规划会话详情",
    response_model=AgentState,
)
def get_trip_session(
    session_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """读取完整状态、动作历史和错误记录，用于复盘。"""

    try:
        # 直接读取最近一次 SQLite 检查点，不会重新执行任何工具。
        return agent_state_store.get_state(session_id, user_id=current_user.user_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/trip/sessions/{session_id}/execution-view",
    summary="查询面向结果页的轻量行程执行视图",
    response_model=TripExecutionView,
)
def get_trip_execution_view(
    session_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """仅返回行程展示、真实路线、时间轴和质量报告，不返回完整 AgentState。"""

    cached = read_model_cache.get_execution_view(
        session_id,
        TripExecutionView,
        user_id=current_user.user_id,
    )
    if cached is not None:
        return cached
    try:
        # Redis 未命中时读取最近检查点并生成投影；数据库仍是事实来源。
        state = agent_state_store.get_state(session_id, user_id=current_user.user_id)
        view = TripExecutionView.from_agent_state(state)
        read_model_cache.set_execution_view(
            session_id,
            view,
            active=state.status != "completed",
            user_id=current_user.user_id,
        )
        return view
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/trip/sessions/{session_id}/drafts",
    summary="创建行程编辑草稿",
    response_model=TripDraft,
)
def create_trip_draft(
    session_id: str,
    payload: TripDraftCreate,
    current_user: User = Depends(current_user_dependency),
):
    """以当前确认版本为基线创建草稿，不触发路线或 LLM 调用。"""
    try:
        agent_state_store.get_state(session_id, user_id=current_user.user_id)
        return trip_draft_service.create_draft(session_id, payload)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/api/trip/sessions/{session_id}/drafts/{draft_id}",
    summary="查询行程草稿",
    response_model=TripDraft,
)
def get_trip_draft(
    session_id: str,
    draft_id: str,
    current_user: User = Depends(current_user_dependency),
):
    try:
        agent_state_store.get_state(session_id, user_id=current_user.user_id)
        return trip_version_store.get_draft(session_id, draft_id)
    except (SessionNotFoundError, DraftNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.put(
    "/api/trip/sessions/{session_id}/drafts/{draft_id}",
    summary="更新行程编辑草稿",
    response_model=TripDraft,
)
def update_trip_draft(
    session_id: str,
    draft_id: str,
    payload: TripDraftUpdate,
    current_user: User = Depends(current_user_dependency),
):
    """继续修改同一草稿；旧候选版本会被标记为 superseded。"""
    try:
        agent_state_store.get_state(session_id, user_id=current_user.user_id)
        return trip_draft_service.update_draft(session_id, draft_id, payload)
    except (SessionNotFoundError, DraftNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/trip/sessions/{session_id}/drafts/{draft_id}/evaluate",
    summary="重新评估行程草稿",
    response_model=DraftEvaluationResponse,
)
def evaluate_trip_draft(
    session_id: str,
    draft_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """只查询变化路线，然后重算时间轴、餐饮、约束与质量分。"""
    try:
        agent_state_store.get_state(session_id, user_id=current_user.user_id)
        return trip_draft_service.evaluate_draft(session_id, draft_id)
    except (SessionNotFoundError, DraftNotFoundError, VersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/api/trip/sessions/{session_id}/drafts/{draft_id}/confirm",
    summary="确认草稿候选版本",
    response_model=ConfirmDraftResponse,
)
def confirm_trip_draft(
    session_id: str,
    draft_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """确认后才更新 AgentState，继续修改则保留原确认版本。"""
    try:
        agent_state_store.get_state(session_id, user_id=current_user.user_id)
        response = trip_draft_service.confirm_draft(session_id, draft_id)
        # 确认版本会更新 AgentState，必须淘汰旧的终态 execution-view 快照。
        read_model_cache.delete_execution_view(
            session_id,
            user_id=current_user.user_id,
        )
        return response
    except (SessionNotFoundError, DraftNotFoundError, VersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/api/trip/sessions/{session_id}/versions",
    summary="查询行程版本列表",
    response_model=list[TripPlanVersion],
)
def list_trip_plan_versions(
    session_id: str,
    current_user: User = Depends(current_user_dependency),
):
    # 先确认会话存在，避免对无效会话返回空列表造成歧义。
    try:
        agent_state_store.get_state(session_id, user_id=current_user.user_id)
        return trip_version_store.list_versions(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/trip/sessions/{session_id}/versions/{version_number}",
    summary="查询指定行程版本",
    response_model=TripPlanVersion,
)
def get_trip_plan_version(
    session_id: str,
    version_number: int,
    current_user: User = Depends(current_user_dependency),
):
    try:
        agent_state_store.get_state(session_id, user_id=current_user.user_id)
        return trip_version_store.get_version(session_id, version_number)
    except (SessionNotFoundError, VersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete(
    "/api/trip/sessions/{session_id}",
    summary="永久删除旅行规划会话",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_trip_session(
    session_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """级联删除当前用户的任务事件、任务、草稿、版本和会话。"""

    try:
        task_ids = agent_state_store.delete_session(
            session_id,
            user_id=current_user.user_id,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    read_model_cache.delete_execution_view(
        session_id,
        user_id=current_user.user_id,
    )
    for task_id in task_ids:
        read_model_cache.delete_task_progress(
            task_id,
            user_id=current_user.user_id,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


_RESTARTABLE_AGENT_STATUSES: frozenset[AgentStatus] = frozenset(
    {
        "failed",
        "budget_exhausted",
        "max_steps_reached",
        "convergence_stopped",
    }
)


def _trip_session_requires_restart(state: AgentState) -> bool:
    """判断恢复请求是否必须创建拥有全新生命周期预算的会话。"""

    if state.status in _RESTARTABLE_AGENT_STATUSES:
        return True
    if state.status not in {"pending", "running"} or state.deadline_at is None:
        return False
    deadline = state.deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc)


@app.post(
    "/api/trip/sessions/{session_id}/resume",
    summary="恢复旅行规划会话",
    response_model=AgentState,
)
def resume_trip_session(
    session_id: str,
    current_user: User = Depends(current_user_dependency),
):
    """继续有效检查点；终止或超时会话则使用原请求重新规划。"""

    try:
        # 步骤 1：加载最近检查点。
        state = agent_state_store.get_state(session_id, user_id=current_user.user_id)
        # 失败、预算耗尽或截止时间已过的检查点没有可用恢复空间。
        # 保留旧会话用于审计，并创建新会话获得独立的步骤、调用和时间预算。
        if _trip_session_requires_restart(state):
            resumed = trip_orchestrator.run(
                state.request,
                user_id=current_user.user_id,
            )
        else:
            # 尚有预算的未完成会话从已有数据继续，不重复已成功的步骤。
            resumed = trip_orchestrator.resume(state)
        # 执行可能推进旧会话，也可能返回新的 session_id；两边缓存都需失效。
        read_model_cache.delete_execution_view(
            session_id,
            user_id=current_user.user_id,
        )
        if resumed.session_id != session_id:
            read_model_cache.delete_execution_view(
                resumed.session_id,
                user_id=current_user.user_id,
            )
        return resumed
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentCheckpointError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentBudgetExceededError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentActionError as exc:
        raise HTTPException(status_code=500, detail=_action_error_detail(exc)) from exc
    except AgentConvergenceError as exc:
        raise HTTPException(
            status_code=409, detail=_convergence_error_detail(exc)
        ) from exc
    except AgentMaxStepsError as exc:
        raise HTTPException(status_code=500, detail=_max_steps_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"旅行规划恢复失败（会话：{session_id}）: {exc}",
        ) from exc


@app.get(
    redis_observability.config.prometheus_path,
    include_in_schema=False,
)
def prometheus_metrics():
    """输出 Prometheus 文本协议；指标只使用固定低基数标签。"""

    if not redis_observability.config.prometheus_enabled:
        raise HTTPException(status_code=404, detail="Prometheus 指标端点未启用")
    payload, content_type = redis_observability.prometheus_payload()
    return Response(content=payload, media_type=content_type)


@app.get("/api/observability/redis", summary="Redis 生产可观测状态")
def redis_observability_snapshot():
    """返回连接池、通知、告警和调优参数的脱敏快照。"""

    return redis_observability.snapshot()


# 健康检查
@app.get("/api/health", summary="服务健康检查")
def health_check():
    # Redis 是非关键组件：不可用时报告 degraded，但主服务继续通过健康检查。
    snapshot = redis_observability.snapshot()
    rag_health = rag_runtime.health_snapshot(probe=True)
    return {
        "status": "ok",
        "message": "旅行助手服务运行正常",
        "degraded": bool(snapshot["degraded"] or rag_health["status"] == "degraded"),
        "components": {
            "redis": snapshot["health"],
            "redis_client": snapshot["client_metrics"],
            "redis_pool": snapshot["pool"],
            "redis_notifications": snapshot["notification_health"],
            "redis_alerts": snapshot["alerts"],
            "cache": {
                "backend": cache_store.backend_name,
                "enabled": cache_store.enabled,
                "schema_version": cache_store.schema_version,
                "metrics": snapshot["cache_metrics"],
                "layers": snapshot["layered_cache_metrics"],
                "amap_business": snapshot["amap_business_cache_metrics"],
            },
            "provider_quota": snapshot["provider_quota_metrics"],
            "qdrant": rag_health["qdrant"],
            "rag": rag_health["rag"],
            "embedding_configured": rag_health["embedding_configured"],
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
