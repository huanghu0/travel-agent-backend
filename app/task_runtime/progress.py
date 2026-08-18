"""异步任务的阶段名称和确定性进度映射。"""

from __future__ import annotations


STAGE_NAMES = {
    "queued": "等待执行",
    "running": "准备执行",
    "search_attractions": "景点搜索",
    "get_weather": "天气查询",
    "search_hotels": "酒店搜索",
    "generate_plan": "行程生成",
    "estimate_routes": "路线查询",
    "evaluate_commute": "单段通勤评估",
    "replace_remote_attraction": "过远景点替换",
    "supplement_attractions": "高德候选补充",
    "optimize_routes": "路线优化",
    "evaluate_schedule": "时间轴评估",
    "optimize_schedule": "日程优化",
    "search_restaurants": "餐饮查询",
    "evaluate_constraints": "可执行性约束评估",
    "optimize_constraints": "约束冲突优化",
    "refill_attractions": "最低景点保障",
    "rebuild_plan_content": "行程内容一致性重建",
    "validate_plan": "行程校验",
    "repair_plan": "行程修复",
    "finish": "完成",
    "cancelled": "已取消",
    "failed": "执行失败",
    "timed_out": "执行超时",
}

# 进度按业务阶段而非简单的 current_step/max_steps 计算，避免本地压缩动作导致倒退。
_ACTION_PROGRESS = {
    "search_attractions": 4.0,
    "get_weather": 10.0,
    "search_hotels": 16.0,
    "generate_plan": 27.0,
    "estimate_routes": 40.0,
    "optimize_routes": 49.0,
    "evaluate_commute": 54.0,
    "replace_remote_attraction": 57.0,
    "supplement_attractions": 58.0,
    "evaluate_schedule": 64.0,
    "optimize_schedule": 69.0,
    "search_restaurants": 74.0,
    "evaluate_constraints": 81.0,
    "optimize_constraints": 85.0,
    "refill_attractions": 87.0,
    "rebuild_plan_content": 90.0,
    "validate_plan": 94.0,
    "repair_plan": 91.0,
    "finish": 99.0,
}


def stage_name(action: str) -> str:
    return STAGE_NAMES.get(action, action)


def action_progress(action: str, *, completed: bool = False) -> float:
    base = _ACTION_PROGRESS.get(action, 1.0)
    if action == "finish" and completed:
        return 100.0
    return min(99.0, base + (2.0 if completed else 0.0))
