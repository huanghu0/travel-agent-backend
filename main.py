from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.agent_runtime import (
    AgentActionError,
    AgentBudgetExceededError,
    AgentMaxStepsError,
    AgentState,
    AgentStatus,
    TripOrchestrator,
)
from app.agents import PlannerAgent
from app.core.config import settings
from app.memory import (
    AgentSessionSummary,
    SessionNotFoundError,
    SQLiteAgentStateStore,
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
# 步骤 3：注册工具白名单。景点、天气、酒店直接调用高德，不经过 LLM。
trip_tool_registry = build_trip_tool_registry(
    planner_agent=planner_agent,
    route_cache=route_cache,
)
# 步骤 4：编排器负责按固定状态机循环执行，并统一应用预算、重试和熔断策略。
trip_orchestrator = TripOrchestrator(
    tool_registry=trip_tool_registry,
    max_steps=settings.AGENT_MAX_STEPS,
    max_attempts_per_action=settings.AGENT_MAX_ATTEMPTS_PER_ACTION,
    max_repair_attempts=settings.AGENT_MAX_REPAIR_ATTEMPTS,
    max_duration_seconds=settings.AGENT_MAX_DURATION_SECONDS,
    max_tool_calls=settings.AGENT_MAX_TOOL_CALLS,
    max_llm_calls=settings.AGENT_MAX_LLM_CALLS,
    retry_base_delay_seconds=settings.AGENT_RETRY_BASE_DELAY_SECONDS,
    retry_max_delay_seconds=settings.AGENT_RETRY_MAX_DELAY_SECONDS,
    retry_jitter_seconds=settings.AGENT_RETRY_JITTER_SECONDS,
    circuit_failure_threshold=settings.AGENT_CIRCUIT_FAILURE_THRESHOLD,
    circuit_recovery_timeout_seconds=settings.AGENT_CIRCUIT_RECOVERY_TIMEOUT_SECONDS,
    state_store=agent_state_store,
)


_STAGE_NAMES = {
    "search_attractions": "景点搜索",
    "get_weather": "天气查询",
    "search_hotels": "酒店搜索",
    "generate_plan": "行程生成",
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
            message="旅行计划生成成功",
            data=state.trip_plan,
            session_id=state.session_id,
            execution_steps=state.current_step,
        )
    # 步骤 3：把运行时异常转换成前端容易定位的 HTTP 错误。
    except AgentBudgetExceededError as exc:
        # 时间、工具调用或 LLM 调用预算耗尽，属于暂时不可用。
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentActionError as exc:
        raise HTTPException(status_code=500, detail=_action_error_detail(exc)) from exc
    except AgentMaxStepsError as exc:
        raise HTTPException(status_code=500, detail=_max_steps_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"旅行规划失败（阶段：智能体执行）: {exc}",
        ) from exc


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
    except AgentBudgetExceededError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AgentActionError as exc:
        raise HTTPException(status_code=500, detail=_action_error_detail(exc)) from exc
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
