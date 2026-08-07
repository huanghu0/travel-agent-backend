import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip().strip("\"").strip("'")


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


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
        _env("CLAUDE_LLM_MAX_TOKENS", "16384") or "16384"
    )

    # Persistent AgentState checkpoints and bounded deterministic loop.
    AGENT_MEMORY_DB_PATH: str = _env(
        "AGENT_MEMORY_DB_PATH", "data/agent_memory.db"
    ) or "data/agent_memory.db"
    AGENT_MAX_STEPS: int = int(_env("AGENT_MAX_STEPS", "24") or "24")
    AGENT_MAX_ATTEMPTS_PER_ACTION: int = int(
        _env("AGENT_MAX_ATTEMPTS_PER_ACTION", "2") or "2"
    )
    AGENT_MAX_REPAIR_ATTEMPTS: int = int(
        _env("AGENT_MAX_REPAIR_ATTEMPTS", "2") or "2"
    )

    AGENT_MAX_ROUTE_OPTIMIZATION_ATTEMPTS: int = int(
        _env("AGENT_MAX_ROUTE_OPTIMIZATION_ATTEMPTS", "1") or "1"
    )
    ROUTE_OPTIMIZATION_MAX_CANDIDATES: int = int(
        _env("ROUTE_OPTIMIZATION_MAX_CANDIDATES", "6") or "6"
    )
    ROUTE_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT: float = float(
        _env("ROUTE_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT", "10") or "10"
    )

    AGENT_MAX_SCHEDULE_OPTIMIZATION_ATTEMPTS: int = int(
        _env("AGENT_MAX_SCHEDULE_OPTIMIZATION_ATTEMPTS", "1") or "1"
    )
    SCHEDULE_OPTIMIZATION_MAX_CANDIDATES: int = int(
        _env("SCHEDULE_OPTIMIZATION_MAX_CANDIDATES", "6") or "6"
    )
    SCHEDULE_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT: float = float(
        _env("SCHEDULE_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT", "10") or "10"
    )
    SCHEDULE_DEFAULT_START_TIME: str = (
        _env("SCHEDULE_DEFAULT_START_TIME", "09:00") or "09:00"
    )
    SCHEDULE_DEFAULT_END_TIME: str = (
        _env("SCHEDULE_DEFAULT_END_TIME", "18:00") or "18:00"
    )
    SCHEDULE_LUNCH_DURATION_MINUTES: int = int(
        _env("SCHEDULE_LUNCH_DURATION_MINUTES", "60") or "60"
    )
    SCHEDULE_ROUTE_BUFFER_MINUTES: int = int(
        _env("SCHEDULE_ROUTE_BUFFER_MINUTES", "10") or "10"
    )
    SCHEDULE_ATTRACTION_BUFFER_MINUTES: int = int(
        _env("SCHEDULE_ATTRACTION_BUFFER_MINUTES", "10") or "10"
    )

    AGENT_MAX_CONSTRAINT_OPTIMIZATION_ATTEMPTS: int = int(
        _env("AGENT_MAX_CONSTRAINT_OPTIMIZATION_ATTEMPTS", "1") or "1"
    )
    CONSTRAINT_OPTIMIZATION_MAX_CANDIDATES: int = int(
        _env("CONSTRAINT_OPTIMIZATION_MAX_CANDIDATES", "8") or "8"
    )
    CONSTRAINT_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT: float = float(
        _env("CONSTRAINT_OPTIMIZATION_MIN_IMPROVEMENT_PERCENT", "10") or "10"
    )
    CONSTRAINT_LUNCH_WINDOW_START: str = (
        _env("CONSTRAINT_LUNCH_WINDOW_START", "11:30") or "11:30"
    )
    CONSTRAINT_LUNCH_WINDOW_END: str = (
        _env("CONSTRAINT_LUNCH_WINDOW_END", "14:00") or "14:00"
    )
    CONSTRAINT_DAILY_ATTRACTION_SOFT_LIMIT: int = int(
        _env("CONSTRAINT_DAILY_ATTRACTION_SOFT_LIMIT", "5") or "5"
    )

    AGENT_MAX_DURATION_SECONDS: float = float(
        _env("AGENT_MAX_DURATION_SECONDS", "180") or "180"
    )
    AGENT_MAX_TOOL_CALLS: int = int(
        _env("AGENT_MAX_TOOL_CALLS", "15") or "15"
    )
    AGENT_MAX_LLM_CALLS: int = int(
        _env("AGENT_MAX_LLM_CALLS", "6") or "6"
    )
    AGENT_RETRY_BASE_DELAY_SECONDS: float = float(
        _env("AGENT_RETRY_BASE_DELAY_SECONDS", "0.5") or "0.5"
    )
    AGENT_RETRY_MAX_DELAY_SECONDS: float = float(
        _env("AGENT_RETRY_MAX_DELAY_SECONDS", "8") or "8"
    )
    AGENT_RETRY_JITTER_SECONDS: float = float(
        _env("AGENT_RETRY_JITTER_SECONDS", "0.25") or "0.25"
    )
    AGENT_CIRCUIT_FAILURE_THRESHOLD: int = int(
        _env("AGENT_CIRCUIT_FAILURE_THRESHOLD", "3") or "3"
    )
    AGENT_CIRCUIT_RECOVERY_TIMEOUT_SECONDS: float = float(
        _env("AGENT_CIRCUIT_RECOVERY_TIMEOUT_SECONDS", "30") or "30"
    )

    # Amap HTTP timeouts (connect timeout, read timeout).
    AMAP_HTTP_CONNECT_TIMEOUT: float = float(
        _env("AMAP_HTTP_CONNECT_TIMEOUT", "3.05") or "3.05"
    )
    AMAP_HTTP_READ_TIMEOUT: float = float(
        _env("AMAP_HTTP_READ_TIMEOUT", "10") or "10"
    )
    # Standardized provider output limits. Invalid and duplicate rows are removed
    # before these limits are applied.
    AMAP_MAX_ATTRACTION_CANDIDATES: int = int(
        _env("AMAP_MAX_ATTRACTION_CANDIDATES", "8") or "8"
    )
    AMAP_MAX_HOTEL_CANDIDATES: int = int(
        _env("AMAP_MAX_HOTEL_CANDIDATES", "6") or "6"
    )
    AMAP_MAX_WEATHER_DAYS: int = int(
        _env("AMAP_MAX_WEATHER_DAYS", "7") or "7"
    )
    AMAP_MAX_ROUTE_LEGS: int = int(
        _env("AMAP_MAX_ROUTE_LEGS", "12") or "12"
    )
    AMAP_ROUTE_CACHE_ENABLED: bool = _env_bool(
        "AMAP_ROUTE_CACHE_ENABLED", True
    )
    AMAP_ROUTE_CACHE_TTL_SECONDS: int = int(
        _env("AMAP_ROUTE_CACHE_TTL_SECONDS", "3600") or "3600"
    )
    AMAP_ROUTE_UNAVAILABLE_CACHE_TTL_SECONDS: int = int(
        _env("AMAP_ROUTE_UNAVAILABLE_CACHE_TTL_SECONDS", "300") or "300"
    )
    # Amap
    AMAP_API_KEY: Optional[str] = _env("AMAP_API_KEY")

    # Unsplash
    UNSPLASH_ACCESS_KEY: Optional[str] = _env("UNSPLASH_ACCESS_KEY")
    UNSPLASH_SECRET_KEY: Optional[str] = _env("UNSPLASH_SECRET_KEY")


settings = Settings()
