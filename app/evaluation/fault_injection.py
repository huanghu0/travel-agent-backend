"""可重复、可审计的端到端故障注入夹具。"""

from __future__ import annotations

import sqlite3
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field, field_validator

from app.tools.models import ToolErrorType
from app.tools.registry import ToolResultError


class FaultMode(str, Enum):
    TIMEOUT = "timeout"
    UPSTREAM = "upstream"
    RATE_LIMIT = "rate_limit"
    AUTHORIZATION = "authorization"
    INVALID_OUTPUT = "invalid_output"
    SQLITE_LOCKED = "sqlite_locked"


class FaultRule(BaseModel):
    """指定目标在第几次调用时产生何种确定性故障。"""

    target: str = Field(min_length=1)
    mode: FaultMode
    call_numbers: list[int] = Field(default_factory=lambda: [1], min_length=1)
    message: str = ""
    injected_output: Any = None

    @field_validator("call_numbers")
    @classmethod
    def validate_call_numbers(cls, value: list[int]) -> list[int]:
        """调用序号必须为正数，去重排序后保持确定性。"""

        if any(call_number < 1 for call_number in value):
            raise ValueError("fault call_numbers must all be greater than or equal to 1")
        return sorted(set(value))


class FaultEvent(BaseModel):
    """一次实际触发的故障，便于测试断言和运行复盘。"""

    target: str
    mode: FaultMode
    call_number: int = Field(ge=1)
    message: str


class FaultScenario(BaseModel):
    """一组可以独立运行的端到端故障规则。"""

    case_id: str = Field(min_length=1)
    description: str
    rules: list[FaultRule] = Field(default_factory=list)


class FaultInjector:
    """按目标调用次数触发故障；默认不做任何随机行为。"""

    def __init__(self, rules: list[FaultRule] | None = None):
        self.rules = list(rules or [])
        self.call_counts: dict[str, int] = {}
        self.events: list[FaultEvent] = []

    def invoke(self, target: str, callback: Callable[..., Any], *args, **kwargs) -> Any:
        call_number = self.call_counts.get(target, 0) + 1
        self.call_counts[target] = call_number
        rule = next(
            (
                item
                for item in self.rules
                if item.target == target and call_number in item.call_numbers
            ),
            None,
        )
        if rule is None:
            return callback(*args, **kwargs)

        message = rule.message or self._default_message(rule.mode, target)
        self.events.append(
            FaultEvent(
                target=target,
                mode=rule.mode,
                call_number=call_number,
                message=message,
            )
        )
        if rule.mode == FaultMode.INVALID_OUTPUT:
            return rule.injected_output
        if rule.mode == FaultMode.TIMEOUT:
            raise TimeoutError(message)
        if rule.mode == FaultMode.AUTHORIZATION:
            raise PermissionError(message)
        if rule.mode == FaultMode.SQLITE_LOCKED:
            raise sqlite3.OperationalError(message)
        if rule.mode == FaultMode.RATE_LIMIT:
            raise ToolResultError(
                message,
                error_type=ToolErrorType.RATE_LIMIT,
                retryable=True,
                provider_code="429",
                provider_message=message,
            )
        raise ToolResultError(
            message,
            error_type=ToolErrorType.UPSTREAM,
            retryable=True,
            provider_code="503",
            provider_message=message,
        )

    def was_triggered(self, target: str, mode: FaultMode | None = None) -> bool:
        return any(
            event.target == target and (mode is None or event.mode == mode)
            for event in self.events
        )

    @staticmethod
    def _default_message(mode: FaultMode, target: str) -> str:
        messages = {
            FaultMode.TIMEOUT: f"Injected timeout for {target}",
            FaultMode.UPSTREAM: f"Injected upstream failure for {target}",
            FaultMode.RATE_LIMIT: f"Injected rate limit for {target}",
            FaultMode.AUTHORIZATION: f"Injected authorization failure for {target}",
            FaultMode.INVALID_OUTPUT: f"Injected invalid output for {target}",
            FaultMode.SQLITE_LOCKED: "database is locked (injected)",
        }
        return messages[mode]


class FaultInjectingProxy:
    """为 SQLite Store、缓存或普通客户端的方法增加非侵入式故障注入。"""

    def __init__(self, target: Any, injector: FaultInjector, *, prefix: str):
        self._target = target
        self._injector = injector
        self._prefix = prefix.rstrip(".")

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if not callable(attribute):
            return attribute
        target_name = f"{self._prefix}.{name}"

        def injected(*args, **kwargs):
            return self._injector.invoke(target_name, attribute, *args, **kwargs)

        return injected


FIXED_FAULT_SCENARIOS = [
    FaultScenario(
        case_id="amap-timeout-once",
        description="高德景点查询首次超时，后续调用恢复，用于验证可重试分类。",
        rules=[
            FaultRule(
                target="search_attractions",
                mode=FaultMode.TIMEOUT,
                call_numbers=[1],
            )
        ],
    ),
    FaultScenario(
        case_id="llm-invalid-output-once",
        description="行程生成首次返回无效结构，用于验证输出校验和有限重试。",
        rules=[
            FaultRule(
                target="generate_plan",
                mode=FaultMode.INVALID_OUTPUT,
                call_numbers=[1],
                injected_output={"invalid": "trip_plan"},
            )
        ],
    ),
    FaultScenario(
        case_id="sqlite-locked-once",
        description="SQLite 首次保存检查点时被锁，后续调用恢复。",
        rules=[
            FaultRule(
                target="sqlite.save_state",
                mode=FaultMode.SQLITE_LOCKED,
                call_numbers=[1],
            )
        ],
    ),
    FaultScenario(
        case_id="amap-rate-limit-once",
        description="高德工具首次限流，用于验证 429 可重试语义。",
        rules=[
            FaultRule(
                target="search_hotels",
                mode=FaultMode.RATE_LIMIT,
                call_numbers=[1],
            )
        ],
    ),
]
