"""Exceptions raised by the deterministic agent runtime."""

from __future__ import annotations

from app.agent_runtime.state import AgentAction, AgentState


class AgentRuntimeError(RuntimeError):
    """Base runtime error carrying the state at the failure point."""

    def __init__(self, message: str, state: AgentState):
        super().__init__(message)
        self.state = state


class AgentActionError(AgentRuntimeError):
    """Raised after an action exhausts its allowed attempts."""

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
    """Raised when the bounded loop cannot reach FINISH in time."""


class AgentBudgetExceededError(AgentRuntimeError):
    """Raised when a persisted lifetime execution budget is exhausted."""

    def __init__(self, reason: str, state: AgentState):
        super().__init__(reason, state)
        self.reason = reason
