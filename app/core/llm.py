"""支持 OpenAI Responses 与 Anthropic Messages 两种协议的 LLM 客户端。"""

import json
from typing import Any, Protocol

from openai import OpenAI

from app.core.config import settings
from app.infrastructure.redis.rate_limit import ProviderQuotaController


class LLMOutputTruncatedError(RuntimeError):
    """模型因为达到输出 Token 上限而提前停止时抛出。"""


class LLMClient(Protocol):
    def invoke(
        self,
        instructions: str,
        input_text: str,
        response_model: type[Any] | None = None,
    ) -> str:
        """根据系统指令和用户输入生成文本结果。"""


def _required(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value


def _normalize_protocol(protocol: str) -> str:
    aliases = {
        "openai": "responses",
        "gpt": "responses",
        "claude": "anthropic",
        "messages": "anthropic",
    }
    normalized = protocol.strip().strip("\"").strip("'").lower()
    return aliases.get(normalized, normalized)


def _anthropic_base_url(base_url: str) -> str:
    """返回 /v1 之前的地址前缀，因为 SDK 会自动追加 /v1/messages。"""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized[:-3]
    return normalized


def _field(value: Any, name: str, default: Any = None) -> Any:
    """统一读取 SDK 对象属性或网关 JSON 字典字段。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _text_value(value: Any) -> str | None:
    text = _field(value, "text")
    return text if isinstance(text, str) and text.strip() else None


def _as_content_blocks(content: Any) -> list[Any]:
    if content is None:
        return []
    if isinstance(content, (list, tuple)):
        return list(content)
    return [content]


def _anthropic_text(response: Any) -> str | None:
    """从官方及常见网关的 Anthropic 响应结构中提取最终文本。"""
    content = _field(response, "content")
    text_parts: list[str] = []

    # 某些中转服务会把 Anthropic content 数组简化为普通字符串。
    if isinstance(content, str):
        if content.strip():
            return content
    else:
        for block in _as_content_blocks(content):
            if isinstance(block, str):
                if block.strip():
                    text_parts.append(block)
                continue

            block_type = _field(block, "type")
            normalized_type = (
                block_type.lower() if isinstance(block_type, str) else None
            )

            # thinking 属于模型内部推理，不能当作最终答案返回。
            if normalized_type in {"thinking", "redacted_thinking"}:
                continue

            # 官方文本块使用 type=text，少数兼容网关可能省略 type。
            if normalized_type in {None, "", "text", "output_text"}:
                text = _text_value(block)
                if text:
                    text_parts.append(text)

    if text_parts:
        return "".join(text_parts)

    # 兼容部分中转服务使用的备用文本字段。
    for field_name in ("output_text", "completion"):
        value = _field(response, field_name)
        if isinstance(value, str) and value.strip():
            return value

    return None


def _anthropic_tool_call(response: Any) -> str | None:
    """把 Anthropic tool_use 块转换成项目统一的 JSON 工具调用格式。"""
    for block in _as_content_blocks(_field(response, "content")):
        block_type = _field(block, "type")
        if not isinstance(block_type, str) or block_type.lower() != "tool_use":
            continue

        name = _field(block, "name")
        tool_input = _field(block, "input")
        if isinstance(name, str) and name and isinstance(tool_input, dict):
            return json.dumps(
                {"type": "tool_call", "name": name, "input": tool_input},
                ensure_ascii=False,
            )
    return None


def _anthropic_tool_continuation(response: Any) -> tuple[list[dict], list[dict]] | None:
    """构造安全的 tool_result 续轮消息，但不会执行模型自行生成的命令。"""
    assistant_content: list[dict] = []
    tool_results: list[dict] = []

    for block in _as_content_blocks(_field(response, "content")):
        if isinstance(block, dict):
            serialized = {key: value for key, value in block.items() if value is not None}
        elif callable(getattr(block, "model_dump", None)):
            serialized = block.model_dump(exclude_none=True)
        else:
            block_type = _field(block, "type")
            serialized = {"type": block_type} if block_type else {}
            for field_name in (
                "id",
                "name",
                "input",
                "text",
                "thinking",
                "signature",
                "data",
            ):
                value = _field(block, field_name)
                if value is not None:
                    serialized[field_name] = value

        if serialized:
            assistant_content.append(serialized)

        if _field(block, "type") == "tool_use":
            tool_use_id = _field(block, "id")
            if isinstance(tool_use_id, str) and tool_use_id:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": (
                            "Tool execution is disabled for this request. "
                            "Continue without tools and return the final JSON object directly."
                        ),
                        "is_error": True,
                    }
                )

    if not assistant_content or not tool_results:
        return None
    return assistant_content, tool_results


def _anthropic_response_diagnostics(response: Any) -> str:
    """只返回安全的结构元数据，避免在异常中泄漏模型生成内容。"""
    content = _field(response, "content")
    blocks = _as_content_blocks(content)
    block_types: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            block_types.append("string")
            continue
        block_type = _field(block, "type")
        block_types.append(str(block_type) if block_type else "untyped")

    tool_names = [
        str(_field(block, "name"))
        for block in blocks
        if _field(block, "type") == "tool_use" and _field(block, "name")
    ]
    details = [
        f"response_type={type(response).__name__}",
        f"stop_reason={_field(response, 'stop_reason') or 'unknown'}",
        f"content_count={len(blocks)}",
        "content_types=" + (",".join(block_types) if block_types else "none"),
    ]
    if tool_names:
        details.append("tool_names=" + ",".join(tool_names))
    response_id = _field(response, "id")
    if isinstance(response_id, str) and response_id:
        details.append(f"response_id={response_id[:100]}")
    return ", ".join(details)


def _acquire_llm_quota(
    controller: ProviderQuotaController | None,
    model: str,
) -> None:
    """在每一次真实模型网络请求前扣减跨实例请求额度。"""

    if controller is not None:
        controller.acquire_llm(model)


class ResponsesLLM:
    """OpenAI 兼容的原生 Responses API 客户端。"""

    def __init__(
        self,
        client: Any | None = None,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        quota_controller: ProviderQuotaController | None = None,
    ):
        self.model = model or settings.GPT_LLM_MODEL_ID
        self.quota_controller = quota_controller
        if client is not None:
            self.client = client
            return

        self.client = OpenAI(
            api_key=_required(api_key or settings.GPT_LLM_API_KEY, "GPT_LLM_API_KEY"),
            base_url=base_url or settings.GPT_LLM_BASE_URL,
            timeout=settings.LLM_TIMEOUT,
            max_retries=0,
        )

    def invoke(
        self,
        instructions: str,
        input_text: str,
        response_model: type[Any] | None = None,
    ) -> str:
        # 步骤 1：组装原生 Responses API 请求，不转换成 Chat Completions 格式。
        request = {
            "model": self.model,
            "instructions": instructions,
            "input": input_text,
        }
        _acquire_llm_quota(self.quota_controller, self.model)
        responses_api = self.client.responses
        parse = getattr(responses_api, "parse", None)
        # 步骤 2：SDK 支持 parse 时直接请求 Pydantic 结构化输出，否则附加 JSON Schema。
        if response_model is not None and callable(parse):
            response = parse(**request, text_format=response_model)
        else:
            if response_model is not None:
                request["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": response_model.__name__.lower(),
                        "schema": response_model.model_json_schema(),
                        "strict": False,
                    }
                }
            response = responses_api.create(**request)

        # 步骤 3：先识别输出截断，再按结构化对象、output_text、内容块依次提取。
        status = getattr(response, "status", None)
        incomplete_details = getattr(response, "incomplete_details", None)
        incomplete_reason = (
            incomplete_details.get("reason")
            if isinstance(incomplete_details, dict)
            else getattr(incomplete_details, "reason", None)
        )
        if status == "incomplete":
            raise LLMOutputTruncatedError(
                "Responses API output was incomplete"
                + (f": {incomplete_reason}" if incomplete_reason else "")
            )

        output_parsed = getattr(response, "output_parsed", None)
        if output_parsed is not None:
            if hasattr(output_parsed, "model_dump_json"):
                return output_parsed.model_dump_json()
            return json.dumps(output_parsed, ensure_ascii=False)

        output_text = getattr(response, "output_text", None)
        if output_text:
            return output_text

        text_parts: list[str] = []
        for item in getattr(response, "output", []) or []:
            contents = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
            for content in contents or []:
                text = _text_value(content)
                if text:
                    text_parts.append(text)

        if text_parts:
            return "".join(text_parts)
        raise RuntimeError("Responses API returned no text output")


class AnthropicLLM:
    """Anthropic 兼容的原生 Messages API 客户端。"""

    def __init__(
        self,
        client: Any | None = None,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
        quota_controller: ProviderQuotaController | None = None,
    ):
        self.model = model or settings.CLAUDE_LLM_MODEL_ID
        self.quota_controller = quota_controller
        self.max_tokens = max_tokens or settings.CLAUDE_LLM_MAX_TOKENS
        if client is not None:
            self.client = client
            return

        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise RuntimeError(
                "Anthropic protocol requires the 'anthropic' package"
            ) from exc

        configured_base_url = base_url or settings.CLAUDE_LLM_BASE_URL
        self.client = Anthropic(
            api_key=_required(
                api_key or settings.CLAUDE_LLM_API_KEY,
                "CLAUDE_LLM_API_KEY",
            ),
            base_url=_anthropic_base_url(configured_base_url),
            timeout=settings.LLM_TIMEOUT,
            max_retries=0,
        )

    def invoke(
        self,
        instructions: str,
        input_text: str,
        response_model: type[Any] | None = None,
    ) -> str:
        if response_model is not None:
            # 此处 Anthropic Messages 没有可直接使用的 Pydantic 响应解析器。
            # 因此只注入精简结构约束；完整嵌套 Schema 会显著增大上下文，
            # 再叠加较大的 max_tokens 时，部分中转网关可能直接拒绝请求。
            model_schema = response_model.model_json_schema()
            required_fields = model_schema.get("required", [])
            required_text = ", ".join(required_fields)
            instructions = (
                f"{instructions}\n\n"
                "Return only one JSON object. Do not use Markdown, explanations, "
                "or an outer wrapper. Do not call tools, shell commands, or write files; "
                "produce the JSON directly in the response."
                + (
                    f" Required top-level fields: {required_text}."
                    if required_text
                    else ""
                )
            )

        # 步骤 1：按原生 Anthropic Messages 协议发送 system 和 messages。
        messages = [{"role": "user", "content": input_text}]
        _acquire_llm_quota(self.quota_controller, self.model)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=instructions,
            messages=messages,
        )

        # 步骤 2：结构化输出请求若意外返回 tool_use，追加一次“工具不可用”的纠正回合。
        stop_reason = _field(response, "stop_reason")
        if stop_reason == "tool_use" and response_model is not None:
            continuation = _anthropic_tool_continuation(response)
            retry_instructions = (
                f"{instructions}\n\n"
                "Tools are unavailable for this structured-output request. "
                "After the tool result, return the requested JSON immediately."
            )
            if continuation is not None:
                assistant_content, tool_results = continuation
                retry_messages = [
                    *messages,
                    {"role": "assistant", "content": assistant_content},
                    {"role": "user", "content": tool_results},
                ]
            else:
                retry_messages = messages

            _acquire_llm_quota(self.quota_controller, self.model)
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=retry_instructions,
                messages=retry_messages,
            )
            stop_reason = _field(response, "stop_reason")

        # 步骤 3：纠正后仍返回工具调用则停止，避免无限续写循环。
        if stop_reason == "tool_use" and response_model is not None:
            raise RuntimeError(
                "Anthropic returned a tool call instead of structured JSON after retry "
                f"({_anthropic_response_diagnostics(response)})"
            )
        if stop_reason == "max_tokens":
            raise LLMOutputTruncatedError(
                "Anthropic output reached max_tokens "
                f"({self.max_tokens}). Increase CLAUDE_LLM_MAX_TOKENS."
            )
        if stop_reason in {"refusal", "content_filter", "safety"}:
            raise RuntimeError(
                f"Anthropic request was refused by the provider: {stop_reason}"
            )
        if stop_reason in {
            "context_length_exceeded",
            "max_context_length",
            "model_context_window_exceeded",
        }:
            raise RuntimeError(
                f"Anthropic context window was exceeded: {stop_reason}"
            )
        # 旧搜索 Agent 期望得到工具调用；Claude 可能返回原生 tool_use 块，
        # 而不是项目早期使用的 [TOOL_CALL:...] 文本格式。
        if stop_reason == "tool_use":
            tool_call = _anthropic_tool_call(response)
            if tool_call:
                return tool_call

        text = _anthropic_text(response)
        if text:
            return text

        raise RuntimeError(
            "Anthropic Messages API returned no text output "
            f"({_anthropic_response_diagnostics(response)})"
        )


_default_quota_controller: ProviderQuotaController | None = None


def configure_llm_quota_controller(
    controller: ProviderQuotaController | None,
) -> None:
    """仅影响后续由 create_llm 创建的生产客户端。"""

    global _default_quota_controller
    _default_quota_controller = controller
    _llm_clients.clear()


def create_llm(protocol: str | None = None) -> LLMClient:
    # 根据 LLM_PROTOCOL 创建与上游接口一致的原生客户端。
    selected = _normalize_protocol(protocol or settings.LLM_PROTOCOL)
    if selected == "responses":
        return ResponsesLLM(quota_controller=_default_quota_controller)
    if selected == "anthropic":
        return AnthropicLLM(quota_controller=_default_quota_controller)
    raise RuntimeError(
        f"Unsupported LLM_PROTOCOL: {selected}. "
        "Expected 'responses' or 'anthropic'."
    )


_llm_clients: dict[str, LLMClient] = {}


def get_llm(protocol: str | None = None) -> LLMClient:
    """按所选协议返回可复用的 LLM 客户端。"""
    selected = _normalize_protocol(protocol or settings.LLM_PROTOCOL)
    # 同一协议复用客户端，避免每个 Agent 动作重复创建连接池。
    if selected not in _llm_clients:
        _llm_clients[selected] = create_llm(selected)
    return _llm_clients[selected]
