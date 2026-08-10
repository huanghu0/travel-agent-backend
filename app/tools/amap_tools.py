"""兼容旧 Agent 的高德工具入口。

生产链路使用 app.providers.amap.AmapProviderClient。这里保留 AmapTools 和
parse_tool_call，避免旧 Agent/外部导入立即失效。
"""

from __future__ import annotations

from typing import Any

import requests

from app.providers.amap.client import AmapClient
from app.providers.amap.errors import AmapProviderError
from app.tools.models import ToolErrorType
from app.tools.registry import ToolResultError


class AmapTools(AmapClient):
    """旧接口兼容层：保持返回高德原始 JSON 和 ToolResultError 语义。"""

    @staticmethod
    def http_get(*args: Any, **kwargs: Any) -> Any:
        # 间接调用模块属性，使现有测试和调用方仍可 patch requests.get。
        return requests.get(*args, **kwargs)

    @classmethod
    def text_search(cls, keywords: str, city: str) -> dict[str, Any]:
        """保留旧版可使用位置参数的调用签名。"""
        return super().text_search(keywords=keywords, city=city)


    @classmethod
    def _get_json(cls, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return super()._get_json(url, params)
        except AmapProviderError as exc:
            raise ToolResultError(
                str(exc),
                error_type=ToolErrorType(exc.kind.value),
                retryable=exc.retryable,
                provider_code=exc.provider_code,
                provider_message=exc.provider_message,
            ) from exc


# 该工具调用解析器只供已经退出主流程的旧版提示词地图智能体兼容使用。
def parse_tool_call(response: str):
    """解析 Anthropic 原生工具调用或旧版文本工具指令。"""
    import json
    import re

    aliases = {
        "maps_text_search": "amap_maps_text_search",
        "maps_weather": "amap_maps_weather",
    }

    try:
        payload = json.loads(response.strip())
    except (json.JSONDecodeError, TypeError, AttributeError):
        payload = None

    if isinstance(payload, dict) and payload.get("type") == "tool_call":
        tool_name = payload.get("name")
        params = payload.get("input")
        if isinstance(tool_name, str) and isinstance(params, dict):
            return aliases.get(tool_name, tool_name), params

    pattern = r"\[TOOL_CALL:(.*?):(.*?)\]"
    match = re.search(pattern, response)
    if match:
        tool_name = aliases.get(match.group(1), match.group(1))
        params = {}
        for param in match.group(2).split(","):
            if "=" not in param:
                continue
            key, value = param.split("=", 1)
            params[key.strip()] = value.strip()
        return tool_name, params
    return None, None
