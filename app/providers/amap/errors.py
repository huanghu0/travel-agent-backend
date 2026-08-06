"""高德 Provider 的独立错误模型，不依赖 ToolRegistry。"""

from __future__ import annotations

from enum import Enum
from typing import Any


class AmapErrorKind(str, Enum):
    INVALID_INPUT = "invalid_input"
    INVALID_OUTPUT = "invalid_output"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UPSTREAM = "upstream"


class AmapProviderError(RuntimeError):
    """可由工具层转换成 ActionResult 的高德标准错误。"""

    def __init__(
        self,
        message: str,
        *,
        kind: AmapErrorKind,
        retryable: bool,
        provider_code: str | None = None,
        provider_message: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.provider_code = provider_code
        self.provider_message = provider_message


_AUTHORIZATION_MARKERS = (
    "invalid_user_key",
    "invalid_user_signature",
    "userkey_plat_nomatch",
    "userkey_suspended",
    "userkey_recycled",
    "invalid_user_ip",
    "invalid_user_domain",
    "invalid key",
    "signature",
    "401",
    "403",
    "无权",
    "鉴权",
)
_RATE_LIMIT_MARKERS = (
    "daily_query_over_limit",
    "access_too_frequent",
    "user_daily_query_over_limit",
    "ip_query_over_limit",
    "rate limit",
    "too many requests",
    "429",
    "限流",
    "超限",
)
_INVALID_INPUT_MARKERS = (
    "invalid_params",
    "missing_required_params",
    "illegal_request",
    "请求参数非法",
    "缺少必填参数",
)


def raise_amap_failure(detail: str, provider_code: str | None = None) -> None:
    normalized = detail.lower()
    if any(marker in normalized for marker in _AUTHORIZATION_MARKERS):
        kind = AmapErrorKind.AUTHORIZATION
        retryable = False
    elif any(marker in normalized for marker in _RATE_LIMIT_MARKERS):
        kind = AmapErrorKind.RATE_LIMIT
        retryable = True
    elif any(marker in normalized for marker in _INVALID_INPUT_MARKERS):
        kind = AmapErrorKind.INVALID_INPUT
        retryable = False
    else:
        kind = AmapErrorKind.UPSTREAM
        retryable = True

    message = f"{detail} (infocode={provider_code})" if provider_code else detail
    raise AmapProviderError(
        message,
        kind=kind,
        retryable=retryable,
        provider_code=provider_code,
        provider_message=detail,
    )


def validate_amap_response(result: Any) -> dict[str, Any]:
    """校验高德 HTTP 200 响应中的业务状态，成功时返回原始对象。"""

    if not isinstance(result, dict):
        raise AmapProviderError(
            f"地图工具返回了无效数据类型: {type(result).__name__}",
            kind=AmapErrorKind.INVALID_OUTPUT,
            retryable=True,
        )

    provider_code_value = result.get("infocode") or result.get("code")
    provider_code = (
        str(provider_code_value).strip() if provider_code_value is not None else None
    )

    error = result.get("error")
    if error:
        raise_amap_failure(str(error), provider_code)

    status = result.get("status")
    if status in (0, "0", False, "failed", "failure", "error"):
        detail = str(
            result.get("info")
            or result.get("message")
            or "地图服务返回失败状态"
        )
        raise_amap_failure(detail, provider_code)

    if result.get("success") is False:
        detail = str(result.get("message") or "地图工具执行失败")
        raise_amap_failure(detail, provider_code)

    return result
