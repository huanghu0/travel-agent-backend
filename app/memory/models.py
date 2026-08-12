"""查询和展示持久化智能体会话使用的公共模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.agent_runtime.state import AgentAction, AgentStatus


class AgentSessionSummary(BaseModel):
    """会话列表使用的轻量摘要。"""

    session_id: str
    status: AgentStatus
    city: str
    current_step: int
    max_steps: int
    action_count: int
    created_at: datetime
    updated_at: datetime


class ExecutionAggregate(BaseModel):
    """一组会话的完成率和步骤基线。"""

    session_count: int = Field(ge=0)
    completed_session_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0.0, le=1.0)
    avg_physical_steps: float = Field(ge=0.0)
    avg_completed_physical_steps: float = Field(ge=0.0)
    avg_logical_actions: float = Field(ge=0.0)
    avg_completed_logical_actions: float = Field(ge=0.0)
    step_compression_rate: float = Field(ge=0.0, le=1.0)


class CityExecutionBaseline(ExecutionAggregate):
    """按目的地城市聚合的执行基线。"""

    city: str
    status_counts: dict[str, int] = Field(default_factory=dict)


class ActionExecutionStats(BaseModel):
    """单个逻辑动作的调用、成功、失败和压缩情况。"""

    action: AgentAction
    execution_count: int = Field(ge=0)
    session_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    compressed_count: int = Field(ge=0)
    compression_rate: float = Field(ge=0.0, le=1.0)
    avg_duration_ms: float = Field(ge=0.0)


class ActionTransitionStats(BaseModel):
    """相邻逻辑动作之间的状态跳转统计。"""

    from_action: AgentAction
    to_action: AgentAction
    transition_count: int = Field(ge=0)
    session_count: int = Field(ge=0)
    completed_session_count: int = Field(ge=0)
    same_physical_step_count: int = Field(ge=0)
    cross_physical_step_count: int = Field(ge=0)


class ActionCycleStats(BaseModel):
    """从某动作出发并再次回到该动作的循环路径统计。"""

    actions: list[AgentAction]
    cycle_count: int = Field(ge=0)
    session_count: int = Field(ge=0)
    completed_session_count: int = Field(ge=0)
    transition_span: int = Field(ge=1)


class ExecutionBaselineReport(BaseModel):
    """状态跳转路径与完成率基线报告。"""

    baseline_version: int = 1
    generated_at: datetime
    requested_limit: int = Field(ge=1)
    matching_session_count: int = Field(ge=0)
    sampled_row_count: int = Field(ge=0)
    analyzed_session_count: int = Field(ge=0)
    invalid_session_count: int = Field(ge=0)
    truncated: bool
    status_filter: AgentStatus | None = None
    city_filter: str | None = None
    max_cycle_span: int = Field(ge=1)
    status_counts: dict[str, int] = Field(default_factory=dict)
    overall: ExecutionAggregate
    cities: list[CityExecutionBaseline] = Field(default_factory=list)
    actions: list[ActionExecutionStats] = Field(default_factory=list)
    common_transitions: list[ActionTransitionStats] = Field(default_factory=list)
    common_cycles: list[ActionCycleStats] = Field(default_factory=list)


class QualityAggregate(BaseModel):
    """一组会话的交付模式、质量分和资源消耗汇总。"""

    session_count: int = Field(ge=0)
    full_completed_count: int = Field(ge=0)
    partial_completed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    in_progress_count: int = Field(ge=0)
    full_completion_rate: float = Field(ge=0.0, le=1.0)
    partial_completion_rate: float = Field(ge=0.0, le=1.0)
    success_rate: float = Field(ge=0.0, le=1.0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    scored_session_count: int = Field(ge=0)
    avg_quality_score: float = Field(ge=0.0, le=100.0)
    avg_physical_steps: float = Field(ge=0.0)
    avg_logical_actions: float = Field(ge=0.0)
    avg_tool_calls: float = Field(ge=0.0)
    avg_llm_calls: float = Field(ge=0.0)
    avg_duration_ms: float = Field(ge=0.0)
    estimated_avoided_llm_repair_calls: int = Field(ge=0)
    estimated_avoided_repair_steps: int = Field(ge=0)


class QualityDimensionBaseline(QualityAggregate):
    """按城市、旅行天数或交通方式聚合的质量基线。"""

    dimension: Literal["city", "travel_days", "transportation", "quality_level"]
    value: str


class QualityIssueStats(BaseModel):
    """最终仍存在的警告或问题代码统计。"""

    issue_code: str
    occurrence_count: int = Field(ge=0)
    session_count: int = Field(ge=0)
    partial_session_count: int = Field(ge=0)


class QualityBaselineReport(BaseModel):
    """部分完成与执行质量可观测性报告。"""

    baseline_version: int = 1
    generated_at: datetime
    requested_limit: int = Field(ge=1)
    matching_session_count: int = Field(ge=0)
    sampled_row_count: int = Field(ge=0)
    analyzed_session_count: int = Field(ge=0)
    invalid_session_count: int = Field(ge=0)
    truncated: bool
    status_filter: AgentStatus | None = None
    city_filter: str | None = None
    travel_days_filter: int | None = Field(default=None, ge=1)
    transportation_filter: str | None = None
    completion_mode_filter: Literal["full", "partial"] | None = None
    quality_level_filter: Literal[
        "excellent", "acceptable", "degraded", "unusable"
    ] | None = None
    overall: QualityAggregate
    cities: list[QualityDimensionBaseline] = Field(default_factory=list)
    travel_days: list[QualityDimensionBaseline] = Field(default_factory=list)
    transportation: list[QualityDimensionBaseline] = Field(default_factory=list)
    quality_levels: list[QualityDimensionBaseline] = Field(default_factory=list)
    common_issue_codes: list[QualityIssueStats] = Field(default_factory=list)
