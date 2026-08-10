"""注册工具和智能体运行时共享的标准模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ToolErrorType(str, Enum):
    TOOL_NOT_FOUND = "tool_not_found"
    INVALID_INPUT = "invalid_input"
    INVALID_OUTPUT = "invalid_output"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UPSTREAM = "upstream"
    EXECUTION = "execution"
    CIRCUIT_OPEN = "circuit_open"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


class ActionResult(BaseModel):
    """所有注册工具执行后返回的统一标准结果。"""

    tool_name: str
    success: bool
    data: Any = None
    error: str | None = None
    error_type: ToolErrorType | None = None
    retryable: bool = False
    duration_ms: int = Field(default=0, ge=0)
    retry_after_ms: int = Field(default=0, ge=0)
    circuit_state: str | None = None
    provider_code: str | None = None
    provider_message: str | None = None


class ToolDescriptor(BaseModel):
    """可安全公开并可供未来协调器使用的工具元数据。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None

