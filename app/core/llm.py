"""Protocol-aware LLM clients for OpenAI Responses and Anthropic Messages."""

from typing import Any, Protocol

from openai import OpenAI

from app.core.config import settings


class LLMOutputTruncatedError(RuntimeError):
    """Raised when a provider stops because the output token limit was reached."""


class LLMClient(Protocol):
    def invoke(self, instructions: str, input_text: str) -> str:
        """Generate text from a system instruction and user input."""


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
    """Return the prefix before /v1 because the SDK appends /v1/messages."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized[:-3]
    return normalized


def _text_value(value: Any) -> str | None:
    if isinstance(value, dict):
        text = value.get("text")
    else:
        text = getattr(value, "text", None)
    return text if isinstance(text, str) and text else None


class ResponsesLLM:
    """OpenAI-compatible native Responses API client."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.model = model or settings.GPT_LLM_MODEL_ID
        if client is not None:
            self.client = client
            return

        self.client = OpenAI(
            api_key=_required(api_key or settings.GPT_LLM_API_KEY, "GPT_LLM_API_KEY"),
            base_url=base_url or settings.GPT_LLM_BASE_URL,
            timeout=settings.LLM_TIMEOUT,
        )

    def invoke(self, instructions: str, input_text: str) -> str:
        response = self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_text,
        )

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
    """Anthropic-compatible native Messages API client."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
    ):
        self.model = model or settings.CLAUDE_LLM_MODEL_ID
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
        )

    def invoke(self, instructions: str, input_text: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=instructions,
            messages=[{"role": "user", "content": input_text}],
        )

        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "max_tokens":
            raise LLMOutputTruncatedError(
                "Anthropic output reached max_tokens "
                f"({self.max_tokens}). Increase CLAUDE_LLM_MAX_TOKENS."
            )

        text_parts: list[str] = []
        for content in getattr(response, "content", []) or []:
            content_type = content.get("type") if isinstance(content, dict) else getattr(content, "type", None)
            if content_type == "text":
                text = _text_value(content)
                if text:
                    text_parts.append(text)

        if text_parts:
            return "".join(text_parts)
        raise RuntimeError("Anthropic Messages API returned no text output")


def create_llm(protocol: str | None = None) -> LLMClient:
    selected = _normalize_protocol(protocol or settings.LLM_PROTOCOL)
    if selected == "responses":
        return ResponsesLLM()
    if selected == "anthropic":
        return AnthropicLLM()
    raise RuntimeError(
        f"Unsupported LLM_PROTOCOL: {selected}. "
        "Expected 'responses' or 'anthropic'."
    )


_llm_clients: dict[str, LLMClient] = {}


def get_llm(protocol: str | None = None) -> LLMClient:
    """Return a reusable client for the selected protocol."""
    selected = _normalize_protocol(protocol or settings.LLM_PROTOCOL)
    if selected not in _llm_clients:
        _llm_clients[selected] = create_llm(selected)
    return _llm_clients[selected]
