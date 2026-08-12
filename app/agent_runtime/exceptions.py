"""确定性智能体运行时抛出的异常类型。"""

from __future__ import annotations

from app.agent_runtime.state import AgentAction, AgentState


class AgentRuntimeError(RuntimeError):
    """携带失败时 AgentState 快照的运行时基础异常。"""

    def __init__(self, message: str, state: AgentState):
        super().__init__(message)
        self.state = state


class AgentCheckpointError(AgentRuntimeError):
    """SQLite 检查点在有限重试后仍无法持久化。"""


class AgentActionError(AgentRuntimeError):
    """某个动作耗尽允许的重试次数后抛出。"""

    def __init__(
        self,
        action: AgentAction,
        message: str,
        state: AgentState,
        *,
        attempt: int,
    ):
        super().__init__(message, state)
        self.action = action
        self.attempt = attempt


class AgentMaxStepsError(AgentRuntimeError):
    """有界执行循环在最大步骤内未能到达 FINISH 时抛出。"""


class AgentBudgetExceededError(AgentRuntimeError):
    """持久化的会话生命周期执行预算耗尽时抛出。"""

    def __init__(self, reason: str, state: AgentState):
        super().__init__(reason, state)
        self.reason = reason


class AgentConvergenceError(AgentRuntimeError):
    """重复动作或连续无收益动作触发提前终止。"""

    def __init__(self, reason: str, state: AgentState):
        super().__init__(reason, state)
        self.reason = reason
