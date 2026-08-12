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
    # LLM 协议：responses（OpenAI Responses API）或 anthropic（Messages API）
    LLM_PROTOCOL: str = (_env("LLM_PROTOCOL", "responses") or "responses").lower()
    LLM_TIMEOUT: int = int(_env("LLM_TIMEOUT", "60") or "60")

    # OpenAI Responses API 配置；旧版 LLM_* 变量仍作为兼容回退。
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

    # Anthropic Messages API 配置。
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

    # AgentState 持久化检查点与有界确定性执行循环。
    AGENT_MEMORY_DB_PATH: str = _env(
        "AGENT_MEMORY_DB_PATH", "data/agent_memory.db"
    ) or "data/agent_memory.db"
    AGENT_MAX_STEPS: int = int(_env("AGENT_MAX_STEPS", "24") or "24")
    AGENT_MAX_ATTEMPTS_PER_ACTION: int = int(
        _env("AGENT_MAX_ATTEMPTS_PER_ACTION", "2") or "2"
    )
    AGENT_MAX_REPEATED_ACTION_INPUTS: int = int(
        _env("AGENT_MAX_REPEATED_ACTION_INPUTS", "1") or "1"
    )
    AGENT_MAX_NO_PROGRESS_STEPS: int = int(
        _env("AGENT_MAX_NO_PROGRESS_STEPS", "3") or "3"
    )
    AGENT_MAX_LOCAL_ACTIONS_PER_STEP: int = int(
        _env("AGENT_MAX_LOCAL_ACTIONS_PER_STEP", "8") or "8"
    )
    AGENT_MAX_REPAIR_ATTEMPTS: int = int(
        _env("AGENT_MAX_REPAIR_ATTEMPTS", "2") or "2"
    )

    # 部分可接受策略只容忍白名单内的非关键校验错误。
    AGENT_PARTIAL_ACCEPTANCE_ENABLED: bool = _env_bool(
        "AGENT_PARTIAL_ACCEPTANCE_ENABLED", True
    )
    AGENT_PARTIAL_ACCEPTANCE_MIN_SCORE: float = float(
        _env("AGENT_PARTIAL_ACCEPTANCE_MIN_SCORE", "70") or "70"
    )
    AGENT_PARTIAL_ACCEPTANCE_MAX_VALIDATION_ERRORS: int = int(
        _env("AGENT_PARTIAL_ACCEPTANCE_MAX_VALIDATION_ERRORS", "2") or "2"
    )
    AGENT_PARTIAL_ACCEPTANCE_MAX_SCHEDULE_OVERTIME_MINUTES: int = int(
        _env(
            "AGENT_PARTIAL_ACCEPTANCE_MAX_SCHEDULE_OVERTIME_MINUTES",
            "60",
        )
        or "60"
    )
    AGENT_PARTIAL_ACCEPTANCE_MAX_UNAVAILABLE_ROUTE_LEGS: int = int(
        _env("AGENT_PARTIAL_ACCEPTANCE_MAX_UNAVAILABLE_ROUTE_LEGS", "0") or "0"
    )
    AGENT_PARTIAL_ACCEPTANCE_MAX_EXCESSIVE_COMMUTE_SEGMENTS: int = int(
        _env(
            "AGENT_PARTIAL_ACCEPTANCE_MAX_EXCESSIVE_COMMUTE_SEGMENTS",
            "0",
        )
        or "0"
    )
    AGENT_PARTIAL_ACCEPTANCE_MAX_CONSTRAINT_ERRORS: int = int(
        _env("AGENT_PARTIAL_ACCEPTANCE_MAX_CONSTRAINT_ERRORS", "0") or "0"
    )
    AGENT_PARTIAL_ACCEPTANCE_MIN_ATTRACTIONS_PER_DAY: int = int(
        _env("AGENT_PARTIAL_ACCEPTANCE_MIN_ATTRACTIONS_PER_DAY", "1") or "1"
    )
    AGENT_PARTIAL_ACCEPTANCE_ALLOWED_ERROR_CODES: tuple[str, ...] = tuple(
        code.strip()
        for code in (
            _env(
                "AGENT_PARTIAL_ACCEPTANCE_ALLOWED_ERROR_CODES",
                "plan.empty_suggestions,schedule.daily_overtime",
            )
            or ""
        ).split(",")
        if code.strip()
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

    # 确定性最低内容保障，以及高德附近候选景点回填。
    AGENT_MIN_TOTAL_ATTRACTIONS: int = int(
        _env("AGENT_MIN_TOTAL_ATTRACTIONS", "2") or "2"
    )
    AGENT_MAX_COMMUTE_REPLACEMENT_ATTEMPTS: int = int(
        _env("AGENT_MAX_COMMUTE_REPLACEMENT_ATTEMPTS", "2") or "2"
    )
    COMMUTE_REPLACEMENT_MAX_CANDIDATES: int = int(
        _env("COMMUTE_REPLACEMENT_MAX_CANDIDATES", "24") or "24"
    )
    AGENT_MAX_COMMUTE_SUPPLEMENT_SEARCHES: int = int(
        _env("AGENT_MAX_COMMUTE_SUPPLEMENT_SEARCHES", "2") or "2"
    )
    COMMUTE_SUPPLEMENT_INITIAL_RADIUS_METERS: int = int(
        _env("COMMUTE_SUPPLEMENT_INITIAL_RADIUS_METERS", "5000") or "5000"
    )
    COMMUTE_SUPPLEMENT_MAX_RADIUS_METERS: int = int(
        _env("COMMUTE_SUPPLEMENT_MAX_RADIUS_METERS", "20000") or "20000"
    )
    COMMUTE_SUPPLEMENT_PAGE_SIZE: int = int(
        _env("COMMUTE_SUPPLEMENT_PAGE_SIZE", "20") or "20"
    )
    COMMUTE_SUPPLEMENT_POOL_MAX_CANDIDATES: int = int(
        _env("COMMUTE_SUPPLEMENT_POOL_MAX_CANDIDATES", "48") or "48"
    )
    COMMUTE_MAX_WALKING_MINUTES: int = int(
        _env("COMMUTE_MAX_WALKING_MINUTES", "45") or "45"
    )
    COMMUTE_MAX_TRANSIT_MINUTES: int = int(
        _env("COMMUTE_MAX_TRANSIT_MINUTES", "90") or "90"
    )
    COMMUTE_MAX_DRIVING_MINUTES: int = int(
        _env("COMMUTE_MAX_DRIVING_MINUTES", "120") or "120"
    )
    AGENT_MAX_CONTENT_REFILL_ATTEMPTS: int = int(
        _env("AGENT_MAX_CONTENT_REFILL_ATTEMPTS", "2") or "2"
    )
    CONTENT_REFILL_MAX_CANDIDATES: int = int(
        _env("CONTENT_REFILL_MAX_CANDIDATES", "24") or "24"
    )
    CONTENT_REFILL_DEFAULT_VISIT_DURATION_MINUTES: int = int(
        _env("CONTENT_REFILL_DEFAULT_VISIT_DURATION_MINUTES", "120") or "120"
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

    # 高德 HTTP 超时配置：连接超时与读取超时。
    AMAP_HTTP_CONNECT_TIMEOUT: float = float(
        _env("AMAP_HTTP_CONNECT_TIMEOUT", "3.05") or "3.05"
    )
    AMAP_HTTP_READ_TIMEOUT: float = float(
        _env("AMAP_HTTP_READ_TIMEOUT", "10") or "10"
    )
    # Provider 标准化输出数量上限；应用上限前会先移除无效项和重复项。
    AMAP_MAX_ATTRACTION_CANDIDATES: int = int(
        _env("AMAP_MAX_ATTRACTION_CANDIDATES", "8") or "8"
    )
    AMAP_MAX_HOTEL_CANDIDATES: int = int(
        _env("AMAP_MAX_HOTEL_CANDIDATES", "6") or "6"
    )
    AMAP_MAX_POI_CANDIDATES: int = int(
        _env("AMAP_MAX_POI_CANDIDATES", "10") or "10"
    )
    AMAP_MAX_RESTAURANT_CANDIDATES_PER_ANCHOR: int = int(
        _env("AMAP_MAX_RESTAURANT_CANDIDATES_PER_ANCHOR", "4") or "4"
    )
    AMAP_MAX_RESTAURANT_SEARCH_ANCHORS: int = int(
        _env("AMAP_MAX_RESTAURANT_SEARCH_ANCHORS", "8") or "8"
    )
    AMAP_RESTAURANT_SEARCH_RADIUS_METERS: int = int(
        _env("AMAP_RESTAURANT_SEARCH_RADIUS_METERS", "2500") or "2500"
    )
    AMAP_RESTAURANT_CACHE_ENABLED: bool = _env_bool(
        "AMAP_RESTAURANT_CACHE_ENABLED", True
    )
    AMAP_RESTAURANT_CACHE_TTL_SECONDS: int = int(
        _env("AMAP_RESTAURANT_CACHE_TTL_SECONDS", "21600") or "21600"
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
    # 高德地图 Web 服务
    AMAP_API_KEY: Optional[str] = _env("AMAP_API_KEY")

    # Unsplash 图片服务
    UNSPLASH_ACCESS_KEY: Optional[str] = _env("UNSPLASH_ACCESS_KEY")
    UNSPLASH_SECRET_KEY: Optional[str] = _env("UNSPLASH_SECRET_KEY")


settings = Settings()
