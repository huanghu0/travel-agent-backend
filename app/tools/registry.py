"""工具白名单：统一完成输入校验、执行、输出校验和错误标准化。"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app.tools.models import ActionResult, ToolDescriptor, ToolErrorType


ToolHandler = Callable[[BaseModel], Any]
ResultValidator = Callable[[Any], Any]


class ToolResultError(RuntimeError):
    """A known tool/output failure with explicit retry semantics."""

    def __init__(
        self,
        message: str,
        *,
        error_type: ToolErrorType = ToolErrorType.UPSTREAM,
        retryable: bool = True,
        provider_code: str | None = None,
        provider_message: str | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.provider_code = provider_code
        self.provider_message = provider_message


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    output_model: type[BaseModel] | None = None
    result_validator: ResultValidator | None = None
    invalid_output_retryable: bool = False
    llm_call_cost: int = 0


class ToolRegistry:
    """Register and execute only explicitly approved tools."""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        # 注册阶段就拒绝空名称、重复名称和非法成本，避免运行时工具表不确定。
        if not definition.name or not definition.name.strip():
            raise ValueError("tool name cannot be empty")
        if definition.name in self._tools:
            raise ValueError(f"tool already registered: {definition.name}")
        if definition.llm_call_cost < 0:
            raise ValueError("llm_call_cost cannot be negative")
        self._tools[definition.name] = definition

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"tool is not registered: {name}") from exc

    def list_names(self) -> list[str]:
        return list(self._tools)

    def llm_call_cost(self, name: str) -> int:
        """Return the declared logical LLM request cost for a tool call."""

        definition = self._tools.get(name)
        return max(0, definition.llm_call_cost) if definition is not None else 0

    def describe(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name=item.name,
                description=item.description,
                input_schema=item.input_model.model_json_schema(),
                output_schema=(
                    item.output_model.model_json_schema()
                    if item.output_model is not None
                    else None
                ),
            )
            for item in self._tools.values()
        ]

    def execute(self, name: str, payload: Any) -> ActionResult:
        started = perf_counter()
        # 步骤 1：只允许执行已经显式注册的工具，阻止模型或调用方执行任意函数。
        definition = self._tools.get(name)
        if definition is None:
            return self._failure(
                name,
                started,
                f"工具未注册或不在白名单中: {name}",
                ToolErrorType.TOOL_NOT_FOUND,
                retryable=False,
            )

        # 步骤 2：使用 Pydantic 校验并转换输入；无效输入不会进入处理器。
        try:
            validated_input = definition.input_model.model_validate(payload)
        except ValidationError as exc:
            return self._failure(
                name,
                started,
                self._safe_message(exc),
                ToolErrorType.INVALID_INPUT,
                retryable=False,
            )

        try:
            # 步骤 3：执行实际处理器，例如高德 API 或 PlannerAgent。
            output = definition.handler(validated_input)
            # 步骤 4：先处理供应商级错误，例如高德 status=0、infocode 等。
            if definition.result_validator is not None:
                output = definition.result_validator(output)
            # 步骤 5：如果声明了输出模型，再执行结构校验并转换为可持久化字典。
            if definition.output_model is not None:
                output = definition.output_model.model_validate(output).model_dump()
        # 步骤 6：已知工具错误保留其错误类型、重试语义和供应商诊断。
        except ToolResultError as exc:
            return self._failure(
                name,
                started,
                self._safe_message(exc),
                exc.error_type,
                retryable=exc.retryable,
                provider_code=exc.provider_code,
                provider_message=exc.provider_message,
            )
        except ValidationError as exc:
            return self._failure(
                name,
                started,
                self._safe_message(exc),
                ToolErrorType.INVALID_OUTPUT,
                retryable=definition.invalid_output_retryable,
            )
        # 未知异常按状态码和关键字归类，统一转换成 ActionResult。
        except Exception as exc:
            error_type, retryable = self._classify_exception(exc)
            return self._failure(
                name,
                started,
                self._safe_message(exc),
                error_type,
                retryable=retryable,
            )

        # 步骤 7：成功和失败都返回同一种 ActionResult，编排器无需了解工具实现细节。
        return ActionResult(
            tool_name=name,
            success=True,
            data=output,
            retryable=False,
            duration_ms=self._elapsed_ms(started),
        )

    @classmethod
    def _failure(
        cls,
        name: str,
        started: float,
        message: str,
        error_type: ToolErrorType,
        *,
        retryable: bool,
        provider_code: str | None = None,
        provider_message: str | None = None,
    ) -> ActionResult:
        return ActionResult(
            tool_name=name,
            success=False,
            error=message,
            error_type=error_type,
            retryable=retryable,
            duration_ms=cls._elapsed_ms(started),
            provider_code=provider_code,
            provider_message=provider_message,
        )

    @staticmethod
    def _classify_exception(exc: Exception) -> tuple[ToolErrorType, bool]:
        if isinstance(exc, TimeoutError):
            return ToolErrorType.TIMEOUT, True
        if isinstance(exc, PermissionError):
            return ToolErrorType.AUTHORIZATION, False

        message = str(exc).lower()
        if any(marker in message for marker in ("401", "403", "unauthorized", "forbidden", "无权", "令牌无效")):
            return ToolErrorType.AUTHORIZATION, False
        if any(marker in message for marker in ("429", "rate limit", "too many requests", "限流")):
            return ToolErrorType.RATE_LIMIT, True
        if any(marker in message for marker in ("timeout", "timed out", "超时")):
            return ToolErrorType.TIMEOUT, True
        if any(marker in message for marker in ("500", "502", "503", "504", "upstream", "上游")):
            return ToolErrorType.UPSTREAM, True
        if any(marker in message for marker in ("400", "bad request", "unsupported", "不支持")):
            return ToolErrorType.INVALID_INPUT, False
        return ToolErrorType.EXECUTION, True

    @staticmethod
    def _safe_message(exc: Exception) -> str:
        message = " ".join(str(exc).split()) or exc.__class__.__name__
        return message[:1000]

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((perf_counter() - started) * 1000))
