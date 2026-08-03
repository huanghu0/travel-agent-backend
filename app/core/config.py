import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip().strip("\"").strip("'")


class Settings:
    # LLM protocol: responses (OpenAI Responses API) or anthropic (Messages API)
    LLM_PROTOCOL: str = (_env("LLM_PROTOCOL", "responses") or "responses").lower()
    LLM_TIMEOUT: int = int(_env("LLM_TIMEOUT", "60") or "60")

    # OpenAI Responses API configuration. Legacy LLM_* variables remain fallbacks.
    GPT_LLM_MODEL_ID: str = _env(
        "GPT_LLM_MODEL_ID", _env("LLM_MODEL_ID", "gpt-4o")
    ) or "gpt-4o"
    GPT_LLM_API_KEY: Optional[str] = _env(
        "GPT_LLM_API_KEY", _env("LLM_API_KEY")
    )
    GPT_LLM_BASE_URL: str = _env(
        "GPT_LLM_BASE_URL",
        _env("LLM_BASE_URL", "https://api.openai.com/v1"),
    ) or "https://api.openai.com/v1"

    # Anthropic Messages API configuration.
    CLAUDE_LLM_MODEL_ID: str = _env(
        "CLAUDE_LLM_MODEL_ID", "claude-opus-4-8"
    ) or "claude-opus-4-8"
    CLAUDE_LLM_API_KEY: Optional[str] = _env(
        "CLAUDE_LLM_API_KEY", _env("LLM_API_KEY")
    )
    CLAUDE_LLM_BASE_URL: str = _env(
        "CLAUDE_LLM_BASE_URL", "https://api.anthropic.com"
    ) or "https://api.anthropic.com"
    CLAUDE_LLM_MAX_TOKENS: int = int(
        _env("CLAUDE_LLM_MAX_TOKENS", "128000") or "128000"
    )

    # Amap
    AMAP_API_KEY: Optional[str] = _env("AMAP_API_KEY")

    # Unsplash
    UNSPLASH_ACCESS_KEY: Optional[str] = _env("UNSPLASH_ACCESS_KEY")
    UNSPLASH_SECRET_KEY: Optional[str] = _env("UNSPLASH_SECRET_KEY")


settings = Settings()
