"""执行循环收敛辅助：稳定指纹、动作输入去重与业务状态进展判断。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.agent_runtime.state import AgentAction, AgentState
from app.routing import plan_route_fingerprint


def _json_value(value: Any) -> Any:
    """把运行时对象递归转换成可稳定排序、可 JSON 序列化的值。"""

    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_value(item) for item in value), key=repr)
    return value


def stable_fingerprint(value: Any) -> str:
    """对业务输入生成跨进程稳定的 SHA-256 指纹。"""

    canonical = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def route_quality_input_fingerprint(request, plan, route_estimates) -> str:
    """绑定路线质量评估实际使用的行程地点与路线快照。"""

    return stable_fingerprint(
        {
            "plan_fingerprint": plan_route_fingerprint(request, plan),
            "route_estimates": route_estimates,
        }
    )


def commute_input_fingerprint(request, plan, route_estimates) -> str:
    """绑定单段通勤评估实际使用的行程地点与路线快照。"""

    return stable_fingerprint(
        {
            "plan_fingerprint": plan_route_fingerprint(request, plan),
            "route_estimates": route_estimates,
        }
    )


def schedule_input_fingerprint(request, plan, route_estimates) -> str:
    """绑定时间轴评估实际使用的行程地点与路线快照。"""

    return stable_fingerprint(
        {
            "plan_fingerprint": plan_route_fingerprint(request, plan),
            "route_estimates": route_estimates,
        }
    )


def constraint_input_fingerprint(
    request,
    plan,
    schedule_report,
    attractions,
    weather,
) -> str:
    """绑定约束评估使用的时间轴、景点事实和天气事实。"""

    return stable_fingerprint(
        {
            "request": request,
            "plan": plan,
            "schedule_report": schedule_report,
            "attractions": attractions,
            "weather": weather,
        }
    )


def validation_input_fingerprint(
    request,
    plan,
    attractions,
    weather,
    hotels,
    route_estimates,
    schedule_report,
    constraint_report,
) -> str:
    """绑定最终校验器使用的全部业务输入，避免复用过期校验结果。"""

    return stable_fingerprint(
        {
            "request": request,
            "plan": plan,
            "attractions": attractions,
            "weather": weather,
            "hotels": hotels,
            "route_estimates": route_estimates,
            "schedule_report": schedule_report,
            "constraint_report": constraint_report,
        }
    )


def business_state_fingerprint(state: AgentState) -> str:
    """生成业务状态指纹；排除步骤、时间戳、重试计数和审计历史。"""

    return stable_fingerprint(
        {
            "attractions": state.attractions,
            "weather": state.weather,
            "hotels": state.hotels,
            "trip_plan": state.trip_plan,
            "route_estimates": state.route_estimates,
            "route_quality_report": state.route_quality_report,
            "route_optimization": {
                "count": state.route_optimization_count,
                "status": state.route_optimization_status,
                "candidate": state.route_optimization_candidate,
            },
            "commute_report": state.commute_report,
            "commute_optimization": {
                "replacement_count": state.commute_replacement_count,
                "supplement_search_count": state.commute_supplement_search_count,
                "status": state.commute_optimization_status,
                "candidate": state.commute_candidate,
                "supplement_query": state.commute_supplement_query,
                "excluded_candidates": state.commute_excluded_candidate_identities,
            },
            "schedule_quality_report": state.schedule_quality_report,
            "schedule_optimization": {
                "count": state.schedule_optimization_count,
                "status": state.schedule_optimization_status,
                "candidate": state.schedule_optimization_candidate,
            },
            "constraint_report": state.constraint_report,
            "constraint_optimization": {
                "count": state.constraint_optimization_count,
                "status": state.constraint_optimization_status,
                "candidate": state.constraint_optimization_candidate,
            },
            "content_refill": {
                "count": state.content_refill_count,
                "status": state.content_refill_status,
                "candidate": state.content_refill_candidate,
                "excluded_candidates": state.content_refill_excluded_identities,
            },
            "repair_count": state.repair_count,
            "plan_consistency_fingerprint": state.plan_consistency_fingerprint,
            "evaluation_input_fingerprints": state.evaluation_input_fingerprints,
            "last_validation_result": state.last_validation_result,
            "finished": state.finished,
        }
    )


def action_input_fingerprint(state: AgentState, action: AgentAction) -> str:
    """只截取动作实际依赖的业务输入，用于发现同输入重复成功执行。"""

    request = state.request
    if action is AgentAction.SEARCH_ATTRACTIONS:
        payload = {"city": request.city, "preferences": request.preferences}
    elif action is AgentAction.GET_WEATHER:
        payload = {"city": request.city}
    elif action is AgentAction.SEARCH_HOTELS:
        payload = {"city": request.city, "accommodation": request.accommodation}
    elif action is AgentAction.GENERATE_PLAN:
        payload = {
            "request": request,
            "attractions": state.attractions,
            "weather": state.weather,
            "hotels": state.hotels,
        }
    elif action is AgentAction.ESTIMATE_ROUTES:
        payload = {
            "request": request,
            "plan": state.trip_plan,
            "attractions": state.attractions,
            "hotels": state.hotels,
        }
    elif action is AgentAction.EVALUATE_COMMUTE:
        payload = commute_input_fingerprint(
            request, state.trip_plan, state.route_estimates
        )
    elif action is AgentAction.EVALUATE_SCHEDULE:
        payload = schedule_input_fingerprint(
            request, state.trip_plan, state.route_estimates
        )
    elif action is AgentAction.EVALUATE_CONSTRAINTS:
        payload = constraint_input_fingerprint(
            request,
            state.trip_plan,
            state.schedule_quality_report,
            state.attractions,
            state.weather,
        )
    elif action is AgentAction.VALIDATE_PLAN:
        payload = validation_input_fingerprint(
            request,
            state.trip_plan,
            state.attractions,
            state.weather,
            state.hotels,
            state.route_estimates,
            state.schedule_quality_report,
            state.constraint_report,
        )
    elif action is AgentAction.REPAIR_PLAN:
        payload = {
            "request": request,
            "plan": state.trip_plan,
            "validation": state.last_validation_result,
            "attractions": state.attractions,
            "weather": state.weather,
            "hotels": state.hotels,
        }
    elif action is AgentAction.SUPPLEMENT_ATTRACTIONS:
        payload = {
            "query": state.commute_supplement_query,
            "attractions": state.attractions,
        }
    elif action is AgentAction.REBUILD_PLAN_CONTENT:
        payload = {
            "request": request,
            "plan": state.trip_plan,
            "routes": state.route_estimates,
            "schedule": state.schedule_quality_report,
            "attractions": state.attractions,
            "weather": state.weather,
            "hotels": state.hotels,
        }
    elif action is AgentAction.FINISH:
        payload = {"validation": state.last_validation_result}
    else:
        # 优化器输入包含候选和基线；使用完整业务状态防止遗漏控制分支。
        payload = business_state_fingerprint(state)
    return stable_fingerprint({"action": action.value, "input": payload})
