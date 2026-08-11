"""从 AgentState 历史生成状态跳转路径和完成率基线。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from app.agent_runtime.state import AgentAction, AgentState, AgentStatus
from app.memory.models import (
    ActionCycleStats,
    ActionExecutionStats,
    ActionTransitionStats,
    CityExecutionBaseline,
    ExecutionAggregate,
    ExecutionBaselineReport,
)


@dataclass
class _ActionAccumulator:
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    compressed_count: int = 0
    duration_ms: int = 0
    sessions: set[str] = field(default_factory=set)


@dataclass
class _TransitionAccumulator:
    transition_count: int = 0
    same_physical_step_count: int = 0
    cross_physical_step_count: int = 0
    sessions: set[str] = field(default_factory=set)
    completed_sessions: set[str] = field(default_factory=set)


@dataclass
class _CycleAccumulator:
    cycle_count: int = 0
    sessions: set[str] = field(default_factory=set)
    completed_sessions: set[str] = field(default_factory=set)


def _safe_rate(numerator: int, denominator: int) -> float:
    """统一生成稳定的小数比例，空样本返回 0。"""

    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _safe_average(total: int, count: int) -> float:
    """统一生成两位小数平均值，避免 API 输出长浮点数。"""

    if count <= 0:
        return 0.0
    return round(total / count, 2)


def _aggregate_states(states: list[AgentState]) -> ExecutionAggregate:
    """计算一组会话的完成率、物理步骤和逻辑动作基线。"""

    completed_states = [state for state in states if state.status == "completed"]
    physical_steps = sum(state.current_step for state in states)
    logical_actions = sum(len(state.action_history) for state in states)
    completed_physical_steps = sum(state.current_step for state in completed_states)
    completed_logical_actions = sum(
        len(state.action_history) for state in completed_states
    )
    compressed_actions = sum(
        int(record.compressed)
        for state in states
        for record in state.action_history
    )
    return ExecutionAggregate(
        session_count=len(states),
        completed_session_count=len(completed_states),
        completion_rate=_safe_rate(len(completed_states), len(states)),
        avg_physical_steps=_safe_average(physical_steps, len(states)),
        avg_completed_physical_steps=_safe_average(
            completed_physical_steps, len(completed_states)
        ),
        avg_logical_actions=_safe_average(logical_actions, len(states)),
        avg_completed_logical_actions=_safe_average(
            completed_logical_actions, len(completed_states)
        ),
        step_compression_rate=_safe_rate(compressed_actions, logical_actions),
    )


def build_execution_baseline(
    states: Iterable[AgentState],
    *,
    requested_limit: int,
    matching_session_count: int,
    sampled_row_count: int,
    invalid_session_count: int = 0,
    status_filter: AgentStatus | None = None,
    city_filter: str | None = None,
    top_n: int = 20,
    max_cycle_span: int = 12,
) -> ExecutionBaselineReport:
    """聚合最近会话，生成可供验收和后续策略判断使用的执行基线。"""

    state_list = list(states)
    safe_top_n = max(1, top_n)
    safe_cycle_span = max(1, max_cycle_span)
    status_counts = Counter(state.status for state in state_list)
    city_states: dict[str, list[AgentState]] = defaultdict(list)
    action_stats: dict[AgentAction, _ActionAccumulator] = defaultdict(
        _ActionAccumulator
    )
    transition_stats: dict[
        tuple[AgentAction, AgentAction], _TransitionAccumulator
    ] = defaultdict(_TransitionAccumulator)
    cycle_stats: dict[tuple[AgentAction, ...], _CycleAccumulator] = defaultdict(
        _CycleAccumulator
    )

    for state in state_list:
        # 步骤 1：按城市保存会话，用于计算城市完成率和平均物理步骤。
        city_states[state.request.city].append(state)
        completed = state.status == "completed"

        # 步骤 2：统计每个逻辑动作的执行、成功、失败、耗时和压缩次数。
        for record in state.action_history:
            accumulator = action_stats[record.action]
            accumulator.execution_count += 1
            accumulator.success_count += int(record.success)
            accumulator.failure_count += int(not record.success)
            accumulator.compressed_count += int(record.compressed)
            accumulator.duration_ms += record.duration_ms
            accumulator.sessions.add(state.session_id)

        # 步骤 3：相邻 ActionRecord 构成一条状态跳转，并区分是否共享物理步骤。
        for previous, current in zip(
            state.action_history, state.action_history[1:]
        ):
            key = (previous.action, current.action)
            accumulator = transition_stats[key]
            accumulator.transition_count += 1
            accumulator.sessions.add(state.session_id)
            if completed:
                accumulator.completed_sessions.add(state.session_id)
            if previous.step == current.step:
                accumulator.same_physical_step_count += 1
            else:
                accumulator.cross_physical_step_count += 1

        # 步骤 4：同一动作再次出现表示状态机回到了旧节点，记录两次出现之间的循环路径。
        last_positions: dict[AgentAction, int] = {}
        actions = [record.action for record in state.action_history]
        for index, action in enumerate(actions):
            previous_index = last_positions.get(action)
            if previous_index is not None:
                transition_span = index - previous_index
                if transition_span <= safe_cycle_span:
                    cycle_key = tuple(actions[previous_index : index + 1])
                    accumulator = cycle_stats[cycle_key]
                    accumulator.cycle_count += 1
                    accumulator.sessions.add(state.session_id)
                    if completed:
                        accumulator.completed_sessions.add(state.session_id)
            last_positions[action] = index

    cities: list[CityExecutionBaseline] = []
    for city, grouped_states in city_states.items():
        aggregate = _aggregate_states(grouped_states)
        cities.append(
            CityExecutionBaseline(
                city=city,
                status_counts=dict(
                    sorted(Counter(state.status for state in grouped_states).items())
                ),
                **aggregate.model_dump(),
            )
        )
    cities.sort(key=lambda item: (-item.session_count, item.city))

    actions = [
        ActionExecutionStats(
            action=action,
            execution_count=accumulator.execution_count,
            session_count=len(accumulator.sessions),
            success_count=accumulator.success_count,
            failure_count=accumulator.failure_count,
            compressed_count=accumulator.compressed_count,
            compression_rate=_safe_rate(
                accumulator.compressed_count, accumulator.execution_count
            ),
            avg_duration_ms=_safe_average(
                accumulator.duration_ms, accumulator.execution_count
            ),
        )
        for action, accumulator in action_stats.items()
    ]
    actions.sort(key=lambda item: (-item.execution_count, item.action.value))

    transitions = [
        ActionTransitionStats(
            from_action=key[0],
            to_action=key[1],
            transition_count=accumulator.transition_count,
            session_count=len(accumulator.sessions),
            completed_session_count=len(accumulator.completed_sessions),
            same_physical_step_count=accumulator.same_physical_step_count,
            cross_physical_step_count=accumulator.cross_physical_step_count,
        )
        for key, accumulator in transition_stats.items()
    ]
    transitions.sort(
        key=lambda item: (
            -item.transition_count,
            item.from_action.value,
            item.to_action.value,
        )
    )

    cycles = [
        ActionCycleStats(
            actions=list(key),
            cycle_count=accumulator.cycle_count,
            session_count=len(accumulator.sessions),
            completed_session_count=len(accumulator.completed_sessions),
            transition_span=len(key) - 1,
        )
        for key, accumulator in cycle_stats.items()
    ]
    cycles.sort(
        key=lambda item: (
            -item.cycle_count,
            item.transition_span,
            tuple(action.value for action in item.actions),
        )
    )

    return ExecutionBaselineReport(
        generated_at=datetime.now(timezone.utc),
        requested_limit=requested_limit,
        matching_session_count=matching_session_count,
        sampled_row_count=sampled_row_count,
        analyzed_session_count=len(state_list),
        invalid_session_count=invalid_session_count,
        truncated=matching_session_count > sampled_row_count,
        status_filter=status_filter,
        city_filter=city_filter,
        max_cycle_span=safe_cycle_span,
        status_counts=dict(sorted(status_counts.items())),
        overall=_aggregate_states(state_list),
        cities=cities,
        actions=actions,
        common_transitions=transitions[:safe_top_n],
        common_cycles=cycles[:safe_top_n],
    )
