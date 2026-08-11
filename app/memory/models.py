"""查询和展示持久化智能体会话使用的公共模型。"""

from datetime import datetime

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
