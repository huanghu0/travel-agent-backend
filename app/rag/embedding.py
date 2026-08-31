"""Dependency-injectable DashScope embedding provider boundary."""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

from openai import OpenAI


logger = logging.getLogger(__name__)


class InvalidEmbeddingError(ValueError):
    """The provider returned an embedding with an invalid shape or value."""


class EmbeddingUnavailableError(RuntimeError):
    """The embedding provider may succeed when retried later."""


class EmbeddingConfigurationError(RuntimeError):
    """The embedding request or provider configuration is invalid."""


class DashScopeEmbeddingClient:
    """Adapt DashScope's OpenAI-compatible API to the embedding protocol."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int,
        timeout_seconds: float,
        max_attempts: int,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.dimension = dimension
        if client is None:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
                max_retries=max(0, max_attempts - 1),
            )
        self._client = client

    def embed(self, text: str) -> list[float]:
        started = time.monotonic()
        try:
            response = self._client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimension,
            )
            values = self._extract_values(response)
            vector = self._validate_values(values)
        except InvalidEmbeddingError:
            self._log("invalid_embedding", started)
            raise
        except Exception as error:
            mapped = self._map_provider_error(error)
            self._log(
                "unavailable" if isinstance(mapped, EmbeddingUnavailableError) else "configuration",
                started,
                provider_code=self._status_code(error),
            )
            raise mapped from None
        self._log("success", started)
        return vector

    def _extract_values(self, response: Any) -> Any:
        data = getattr(response, "data", None)
        if not data:
            raise InvalidEmbeddingError("embedding response did not contain values")
        values = getattr(data[0], "embedding", None)
        if values is None:
            raise InvalidEmbeddingError("embedding response did not contain values")
        return values

    def _validate_values(self, values: Any) -> list[float]:
        try:
            vector = [float(value) for value in values]
        except (TypeError, ValueError, OverflowError):
            raise InvalidEmbeddingError("embedding values were not numeric") from None
        if len(vector) != self.dimension:
            raise InvalidEmbeddingError("embedding dimension did not match configuration")
        if not all(math.isfinite(value) for value in vector):
            raise InvalidEmbeddingError("embedding values were not finite")
        return vector

    def _map_provider_error(self, error: Exception) -> Exception:
        code = self._status_code(error)
        name = type(error).__name__.lower()
        if code == 429 or code is not None and 500 <= code <= 599:
            return EmbeddingUnavailableError(self._safe_message("provider unavailable", code))
        if code is not None and 400 <= code <= 499:
            return EmbeddingConfigurationError(self._safe_message("embedding request rejected", code))
        if isinstance(error, TimeoutError) or "timeout" in name:
            return EmbeddingUnavailableError("embedding provider timed out")
        return EmbeddingUnavailableError("embedding provider unavailable")

    def _status_code(self, error: Exception) -> int | None:
        for value in (getattr(error, "code", None), getattr(error, "status_code", None)):
            if isinstance(value, int):
                return value
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
        match = re.search(r"\b([45]\d\d)\b", str(error))
        return int(match.group(1)) if match else None

    def _safe_message(self, message: str, code: int | None) -> str:
        return f"{message} (provider_status={code})" if code is not None else message

    def _log(self, outcome: str, started: float, *, provider_code: int | None = None) -> None:
        logger.info(
            "dashscope_embedding model=%s outcome=%s duration_ms=%.2f error_kind=%s provider_status=%s",
            self.model,
            "success" if outcome == "success" else "error",
            (time.monotonic() - started) * 1000,
            outcome,
            provider_code,
        )
