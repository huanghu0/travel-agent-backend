from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

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
from app.evaluation import (
    AcceptanceScenario,
    FIXED_ACCEPTANCE_SCENARIOS,
    FixedAcceptanceBaselineReport,
)
from app.memory import (
    AgentSessionSummary,
    ExecutionBaselineReport,
    QualityBaselineReport,
    SessionNotFoundError,
    SQLiteAgentStateStore,
    SQLiteRestaurantCache,
    SQLiteRouteCache,
)
from app.schemas.trip_schema import TripPlanResponse, TripRequest
from app.tools import ToolDescriptor, build_trip_tool_registry
from app.tools.unsplash_tools import get_place_photo

# 初始化FastAPI应用
app = FastAPI(
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

# 初始化规划智能体、SQLite 会话记忆和确定性编排器（单例）
# 步骤 1：PlannerAgent 只负责“生成行程”和“修复行程”，这两个步骤才会调用 LLM。
planner_agent = PlannerAgent()
# 步骤 2：SQLite 保存每次执行的 AgentState，支持查询、复盘和断点恢复。
agent_state_store = SQLiteAgentStateStore(settings.AGENT_MEMORY_DB_PATH)
route_cache = (
    SQLiteRouteCache(settings.AGENT_MEMORY_DB_PATH)
    if settings.AMAP_ROUTE_CACHE_ENABLED
    else None
)
# 餐饮缓存与会话记忆共用数据库文件，但使用独立表和较短 TTL。
restaurant_cache = (
    SQLiteRestaurantCache(settings.AGENT_MEMORY_DB_PATH)
    if settings.AMAP_RESTAURANT_CACHE_ENABLED
    else None
)
# 步骤 3：注册工具白名单。景点、天气、酒店直接调用高德，不经过 LLM。
trip_tool_registry = build_trip_tool_registry(
    planner_agent=planner_agent,
    route_cache=route_cache,
    restaurant_cache=restaurant_cache,
)
# 步骤 4：编排器负责按固定状态机循环执行，并统一应用预算、重试和熔断策略。
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
def get_poi_photo(name: str):
    """根据景点名称获取Unsplash图片。"""
    return get_place_photo(name)


@app.get(
    "/api/agent/tools",
    summary="查询智能体可用工具白名单",
    response_model=list[ToolDescriptor],
)
def list_agent_tools():
    """返回安全工具元数据，不包含处理器实现和密钥。"""

    return trip_tool_registry.describe()


@app.post("/api/trip/plan", summary="生成旅行计划", response_model=TripPlanResponse)
async def generate_trip_plan(request: TripRequest):
    """
    使用确定性、有界循环生成旅行计划，并在每一步写入SQLite检查点。
    """
    try:
        # 步骤 1：在线程池中运行同步编排循环，避免高德和 LLM 的阻塞请求卡住 FastAPI 事件循环。
        state = await run_in_threadpool(trip_orchestrator.run, request)

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
):
    """从 SQLite 最近会话统计完成率、动作次数、跳转路径和常见循环。"""

    # 该接口只读取历史检查点，不执行高德、LLM 或任何行程修复动作。
    return agent_state_store.get_execution_baseline(
        limit=limit,
        status=status,
        city=city,
        top_n=top_n,
        max_cycle_span=max_cycle_span,
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
    )


@app.get(
    "/api/agent/analytics/fixed-acceptance-scenarios",
    summary="查询固定端到端验收场景",
    response_model=list[AcceptanceScenario],
)
def list_fixed_acceptance_scenarios():
    """返回五城市、1/3/5 日和三类交通方式的固定请求清单。"""

    return FIXED_ACCEPTANCE_SCENARIOS


@app.get(
    "/api/agent/analytics/fixed-acceptance-baseline",
    summary="查询固定端到端验收覆盖率和通过率",
    response_model=FixedAcceptanceBaselineReport,
)
def get_fixed_acceptance_baseline(
    limit: int = Query(default=5000, ge=1, le=10000),
):
    """用每个固定场景最近一次匹配会话执行离线确定性验收。"""

    return agent_state_store.get_fixed_acceptance_baseline(limit=limit)


@app.get(
    "/api/trip/sessions",
    summary="查询旅行规划会话列表",
    response_model=list[AgentSessionSummary],
)
def list_trip_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    status: AgentStatus | None = Query(default=None),
):
    """按最近更新时间倒序查询会话摘要。"""

    return agent_state_store.list_sessions(limit=limit, status=status)


@app.get(
    "/api/trip/sessions/{session_id}",
    summary="查询旅行规划会话详情",
    response_model=AgentState,
)
def get_trip_session(session_id: str):
    """读取完整状态、动作历史和错误记录，用于复盘。"""

    try:
        # 直接读取最近一次 SQLite 检查点，不会重新执行任何工具。
        return agent_state_store.get_state(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/trip/sessions/{session_id}/resume",
    summary="恢复旅行规划会话",
    response_model=AgentState,
)
def resume_trip_session(session_id: str):
    """从最近检查点继续执行，不重复已经成功完成的动作。"""

    try:
        # 步骤 1：加载最近检查点。
        state = agent_state_store.get_state(session_id)
        # 步骤 2：编排器根据已存在的数据决定下一动作，不重复已成功的步骤。
        return trip_orchestrator.resume(state)
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


# 健康检查
@app.get("/api/health", summary="服务健康检查")
def health_check():
    return {"status": "ok", "message": "旅行助手服务运行正常"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
