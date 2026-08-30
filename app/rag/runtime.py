"""Fail-open construction and health state for optional shared-guide RAG."""

from __future__ import annotations

import inspect
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from app.observability.rag_metrics import RagMetrics, rag_metrics
from app.rag.embedding import DashScopeEmbeddingClient
from app.rag.qdrant_index import (
    QdrantSchemaMismatchError,
    QdrantSharedGuideIndex,
    create_qdrant_client,
)
from app.rag.retrieval import NoOpRagRetriever, RagRetrievalService
from app.rag.text_builder import EmbeddingTextBuilder
from app.sharing.worker import ShareIndexWorker


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RagHealthState:
    """Mutable, sanitized optional-runtime health state."""

    status: str
    qdrant: str
    rag: str
    embedding_configured: bool
    reasons: tuple[str, ...] = ()
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "qdrant": self.qdrant,
                "rag": self.rag,
                "embedding_configured": self.embedding_configured,
                "status": self.status,
                "reasons": list(self.reasons),
            }

    def degrade(self, reason: str, *, qdrant: bool = False, rag_enabled: bool) -> None:
        with self._lock:
            self.status = "degraded"
            if qdrant:
                self.qdrant = "degraded"
            if rag_enabled:
                self.rag = "degraded"
            if reason not in self.reasons:
                self.reasons = (*self.reasons, reason)


class _ObservedEmbeddingClient:
    def __init__(
        self,
        delegate: Any,
        *,
        metrics: RagMetrics | None,
        on_failure: Callable[[str], None],
    ) -> None:
        self._delegate = delegate
        self._metrics = metrics
        self._on_failure = on_failure
        self.model = delegate.model
        self.dimension = delegate.dimension

    def embed(self, text: str) -> list[float]:
        started = time.monotonic()
        outcome = "success"
        try:
            return self._delegate.embed(text)
        except Exception:
            outcome = "failure"
            self._on_failure("embedding_unavailable")
            raise
        finally:
            if self._metrics is not None:
                self._metrics.embedding_outcomes.labels(outcome=outcome).inc()
                self._metrics.embedding_duration.labels(outcome=outcome).observe(
                    max(0.0, time.monotonic() - started)
                )


class _ObservedVectorIndex:
    _OPERATIONS = frozenset({"query", "upsert", "delete"})

    def __init__(
        self,
        delegate: Any,
        *,
        metrics: RagMetrics | None,
        on_failure: Callable[[str], None],
    ) -> None:
        self._delegate = delegate
        self._metrics = metrics
        self._on_failure = on_failure

    def __getattr__(self, name: str):
        value = getattr(self._delegate, name)
        if name not in self._OPERATIONS or not callable(value):
            return value

        def observed(*args, **kwargs):
            started = time.monotonic()
            outcome = "success"
            try:
                return value(*args, **kwargs)
            except Exception:
                outcome = "failure"
                self._on_failure("qdrant_unavailable")
                raise
            finally:
                if self._metrics is not None:
                    self._metrics.qdrant_operations.labels(
                        operation=name,
                        outcome=outcome,
                    ).inc()
                    self._metrics.qdrant_duration.labels(
                        operation=name,
                        outcome=outcome,
                    ).observe(max(0.0, time.monotonic() - started))

        return observed


@dataclass(slots=True)
class RagRuntime:
    """Optional adapters, retriever, worker, and non-fatal health state."""

    retriever: Any
    embedding_client: Any | None
    vector_index: Any | None
    health: RagHealthState
    worker: Any | None = None
    _qdrant_client: Any | None = field(default=None, repr=False)
    _qdrant_timeout_seconds: float = field(default=5.0, repr=False)
    _rag_enabled: bool = field(default=False, repr=False)
    _adapters_ready: bool = field(default=False, repr=False)
    _embedding_failed: bool = field(default=False, repr=False)
    _started: bool = field(default=False, repr=False)
    _stopped: bool = field(default=False, repr=False)
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
    )

    @property
    def status(self) -> str:
        return self.health.snapshot()["status"]

    @property
    def embedding_configured(self) -> bool:
        return self.health.embedding_configured

    @property
    def ready(self) -> bool:
        return self.status == "ready" and self._adapters_ready

    @classmethod
    def from_settings(
        cls,
        *,
        settings: Any,
        shared_store: Any | None,
        embedding_client_factory: Callable[..., Any] = DashScopeEmbeddingClient,
        qdrant_client_factory: Callable[..., Any] = create_qdrant_client,
        vector_index_factory: Callable[..., Any] = QdrantSharedGuideIndex,
        worker_factory: Callable[..., Any] = ShareIndexWorker,
        metrics: RagMetrics | None = rag_metrics,
    ) -> "RagRuntime":
        share_enabled = bool(settings.SHARE_SQUARE_ENABLED)
        rag_enabled = bool(settings.RAG_ENABLED)
        feature_enabled = share_enabled or rag_enabled
        worker_enabled = bool(
            getattr(settings, "SHARE_INDEX_WORKER_ENABLED", False)
        )
        embedding_configured = cls._embedding_configured(settings)

        if not feature_enabled:
            return cls(
                retriever=NoOpRagRetriever(),
                embedding_client=None,
                vector_index=None,
                worker=None,
                health=RagHealthState(
                    status="disabled",
                    qdrant="disabled",
                    rag="disabled",
                    embedding_configured=embedding_configured,
                ),
                _qdrant_timeout_seconds=cls._qdrant_timeout(settings),
                _rag_enabled=False,
            )

        try:
            config_errors = list(settings.validate_rag_settings())
        except Exception:
            config_errors = ["invalid_rag_configuration"]
        config_errors = [
            error
            for error in config_errors
            if cls._configuration_error_applies(
                error,
                share_enabled=share_enabled,
                rag_enabled=rag_enabled,
                feature_enabled=feature_enabled,
                worker_enabled=worker_enabled,
            )
        ]
        config_errors.extend(
            cls._runtime_configuration_errors(
                settings,
                share_enabled=share_enabled,
                rag_enabled=rag_enabled,
                feature_enabled=feature_enabled,
                worker_enabled=worker_enabled,
            )
        )
        if config_errors:
            reasons = tuple(cls._configuration_reason(error) for error in config_errors)
            return cls._degraded(
                settings,
                rag_enabled=rag_enabled,
                embedding_configured=embedding_configured,
                reasons=tuple(dict.fromkeys(reasons)),
            )
        if shared_store is None:
            return cls._degraded(
                settings,
                rag_enabled=rag_enabled,
                embedding_configured=embedding_configured,
                reasons=("shared_store_unavailable",),
            )

        health = RagHealthState(
            status="degraded",
            qdrant="degraded",
            rag="degraded" if rag_enabled else "disabled",
            embedding_configured=embedding_configured,
        )
        runtime = cls(
            retriever=NoOpRagRetriever(),
            embedding_client=None,
            vector_index=None,
            worker=None,
            health=health,
            _qdrant_timeout_seconds=cls._qdrant_timeout(settings),
            _rag_enabled=rag_enabled,
        )
        try:
            embedding = embedding_client_factory(
                api_key=settings.DASHSCOPE_API_KEY,
                base_url=settings.DASHSCOPE_BASE_URL,
                model=settings.EMBEDDING_MODEL,
                dimension=settings.EMBEDDING_DIMENSION,
                timeout_seconds=settings.EMBEDDING_TIMEOUT_SECONDS,
                max_attempts=settings.EMBEDDING_MAX_ATTEMPTS,
            )
            qdrant_client = qdrant_client_factory(
                url=settings.QDRANT_URL,
                api_key=settings.QDRANT_API_KEY,
                timeout_seconds=settings.QDRANT_TIMEOUT_SECONDS,
            )
            index = vector_index_factory(
                client=qdrant_client,
                collection=settings.QDRANT_COLLECTION,
                dimension=settings.EMBEDDING_DIMENSION,
            )
            index.ensure_collection()
        except QdrantSchemaMismatchError:
            health.reasons = ("qdrant_schema_mismatch",)
            runtime._qdrant_client = locals().get("qdrant_client")
            logger.warning("rag_runtime status=degraded reason=qdrant_schema_mismatch")
            return runtime
        except Exception as error:
            reason = "adapter_initialization_failed"
            health.reasons = (reason,)
            runtime._qdrant_client = locals().get("qdrant_client")
            logger.warning(
                "rag_runtime status=degraded reason=%s error_class=%s",
                reason,
                type(error).__name__,
            )
            return runtime

        observed_embedding = _ObservedEmbeddingClient(
            embedding,
            metrics=metrics,
            on_failure=runtime._embedding_failure,
        )
        observed_index = _ObservedVectorIndex(
            index,
            metrics=metrics,
            on_failure=runtime._qdrant_failure,
        )
        runtime.embedding_client = observed_embedding
        runtime.vector_index = observed_index
        runtime._qdrant_client = qdrant_client
        runtime._adapters_ready = True
        health.status = "ready"
        health.qdrant = "ready"
        health.rag = "ready" if rag_enabled else "disabled"
        health.reasons = ()

        try:
            if rag_enabled:
                runtime.retriever = RagRetrievalService(
                    embedding_client=observed_embedding,
                    vector_index=observed_index,
                    store=shared_store,
                    text_builder=EmbeddingTextBuilder(),
                    enabled=True,
                    embedding_model=settings.EMBEDDING_MODEL,
                    top_k=settings.RAG_TOP_K,
                    max_top_k=settings.RAG_MAX_TOP_K,
                    candidate_limit=settings.RAG_CANDIDATE_LIMIT,
                    min_score=settings.RAG_MIN_SCORE,
                    reference_max_chars=settings.RAG_REFERENCE_MAX_CHARS,
                    metrics=metrics,
                )
            if worker_enabled:
                worker_kwargs = {
                    "store": shared_store,
                    "embedding_client": observed_embedding,
                    "vector_index": observed_index,
                    "worker_id": f"share-index:{uuid4()}",
                    "poll_seconds": settings.SHARE_INDEX_WORKER_POLL_SECONDS,
                    "lease_seconds": settings.SHARE_INDEX_LEASE_SECONDS,
                    "max_attempts": settings.SHARE_INDEX_MAX_ATTEMPTS,
                    "retry_base_seconds": settings.SHARE_INDEX_RETRY_BASE_SECONDS,
                    "retry_max_seconds": settings.SHARE_INDEX_RETRY_MAX_SECONDS,
                    "shutdown_timeout_seconds": (
                        settings.SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS
                    ),
                }
                if cls._factory_accepts_keyword(worker_factory, "metrics"):
                    worker_kwargs["metrics"] = metrics
                runtime.worker = worker_factory(**worker_kwargs)
        except Exception as error:
            runtime.retriever = NoOpRagRetriever()
            runtime.embedding_client = None
            runtime.vector_index = None
            runtime.worker = None
            runtime._adapters_ready = False
            health.status = "degraded"
            health.qdrant = "degraded"
            health.rag = "degraded" if rag_enabled else "disabled"
            health.reasons = ("runtime_initialization_failed",)
            logger.warning(
                "rag_runtime status=degraded reason=runtime_initialization_failed error_class=%s",
                type(error).__name__,
            )
        return runtime

    @classmethod
    def _degraded(
        cls,
        settings: Any,
        *,
        rag_enabled: bool,
        embedding_configured: bool,
        reasons: tuple[str, ...],
    ) -> "RagRuntime":
        return cls(
            retriever=NoOpRagRetriever(),
            embedding_client=None,
            vector_index=None,
            worker=None,
            health=RagHealthState(
                status="degraded",
                qdrant="degraded",
                rag="degraded" if rag_enabled else "disabled",
                embedding_configured=embedding_configured,
                reasons=reasons,
            ),
            _qdrant_timeout_seconds=cls._qdrant_timeout(settings),
            _rag_enabled=rag_enabled,
        )

    @staticmethod
    def _embedding_configured(settings: Any) -> bool:
        return bool(
            str(getattr(settings, "DASHSCOPE_API_KEY", "") or "").strip()
            and str(getattr(settings, "DASHSCOPE_BASE_URL", "") or "").strip()
            and (
                getattr(settings, "EMBEDDING_MODEL", None)
                == "qwen3.7-text-embedding"
            )
            and getattr(settings, "EMBEDDING_DIMENSION", None) == 768
        )

    @staticmethod
    def _qdrant_timeout(settings: Any) -> float:
        try:
            value = float(settings.QDRANT_TIMEOUT_SECONDS)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 5.0
        return value if math.isfinite(value) and value > 0 else 5.0

    @classmethod
    def _runtime_configuration_errors(
        cls,
        settings: Any,
        *,
        share_enabled: bool,
        rag_enabled: bool,
        feature_enabled: bool,
        worker_enabled: bool,
    ) -> list[str]:
        """Validate constructor inputs before creating any provider client."""

        errors: list[str] = []

        def positive(name: str) -> bool:
            value = getattr(settings, name, None)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                return False
            return math.isfinite(numeric) and numeric > 0

        def integer_at_least(name: str, minimum: int = 1) -> bool:
            value = getattr(settings, name, None)
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= minimum
            )

        if not str(getattr(settings, "QDRANT_URL", "") or "").strip():
            errors.append("invalid_qdrant_url")
        if not str(getattr(settings, "QDRANT_COLLECTION", "") or "").strip():
            errors.append("invalid_qdrant_collection")
        if not str(getattr(settings, "DASHSCOPE_API_KEY", "") or "").strip():
            errors.append("invalid_dashscope_api_key")
        if not str(getattr(settings, "DASHSCOPE_BASE_URL", "") or "").strip():
            errors.append("invalid_dashscope_base_url")
        if (
            getattr(settings, "EMBEDDING_MODEL", None)
            != "qwen3.7-text-embedding"
        ):
            errors.append("invalid_embedding_model")
        if getattr(settings, "EMBEDDING_DIMENSION", None) != 768:
            errors.append("invalid_embedding_dimension")
        if not positive("QDRANT_TIMEOUT_SECONDS"):
            errors.append("invalid_qdrant_timeout_seconds")
        if not positive("EMBEDDING_TIMEOUT_SECONDS"):
            errors.append("invalid_embedding_timeout_seconds")
        if not integer_at_least("EMBEDDING_MAX_ATTEMPTS"):
            errors.append("invalid_embedding_max_attempts")

        if rag_enabled:
            if not integer_at_least("RAG_TOP_K"):
                errors.append("invalid_rag_top_k")
            max_top_k = getattr(settings, "RAG_MAX_TOP_K", None)
            if not integer_at_least("RAG_MAX_TOP_K") or max_top_k > 5:
                errors.append("invalid_rag_max_top_k")
            elif (
                integer_at_least("RAG_TOP_K")
                and getattr(settings, "RAG_TOP_K") > max_top_k
            ):
                errors.append("invalid_rag_top_k")
            if not integer_at_least("RAG_CANDIDATE_LIMIT"):
                errors.append("invalid_rag_candidate_limit")
            minimum_score = getattr(settings, "RAG_MIN_SCORE", None)
            try:
                normalized_score = float(minimum_score)
            except (TypeError, ValueError, OverflowError):
                normalized_score = None
            if (
                not isinstance(minimum_score, (int, float))
                or isinstance(minimum_score, bool)
                or normalized_score is None
                or not math.isfinite(normalized_score)
                or not -1.0 <= normalized_score <= 1.0
            ):
                errors.append("invalid_rag_min_score")
            if not integer_at_least("RAG_REFERENCE_MAX_CHARS"):
                errors.append("invalid_rag_reference_max_chars")

        service_settings_enabled = share_enabled or (
            feature_enabled and worker_enabled
        )
        worker_settings_enabled = feature_enabled and worker_enabled
        if service_settings_enabled:
            if not positive("SHARE_INDEX_LEASE_SECONDS"):
                errors.append("invalid_share_index_lease_seconds")
            if not integer_at_least("SHARE_INDEX_MAX_ATTEMPTS"):
                errors.append("invalid_share_index_max_attempts")
            if not positive("SHARE_INDEX_RETRY_BASE_SECONDS"):
                errors.append("invalid_share_index_retry_base_seconds")
            if not positive("SHARE_INDEX_RETRY_MAX_SECONDS"):
                errors.append("invalid_share_index_retry_max_seconds")
            retry_base = getattr(settings, "SHARE_INDEX_RETRY_BASE_SECONDS", None)
            retry_max = getattr(settings, "SHARE_INDEX_RETRY_MAX_SECONDS", None)
            if positive("SHARE_INDEX_RETRY_BASE_SECONDS") and positive(
                "SHARE_INDEX_RETRY_MAX_SECONDS"
            ) and retry_base > retry_max:
                errors.append("invalid_share_index_retry_range")
        if worker_settings_enabled:
            if not positive("SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS"):
                errors.append("invalid_share_index_shutdown_timeout_seconds")
            if not positive("SHARE_INDEX_WORKER_POLL_SECONDS"):
                errors.append("invalid_share_index_worker_poll_seconds")
        if share_enabled and (
            hasattr(settings, "SHARE_LIST_DEFAULT_LIMIT")
            or hasattr(settings, "SHARE_LIST_MAX_LIMIT")
        ):
            default_limit = getattr(settings, "SHARE_LIST_DEFAULT_LIMIT", None)
            max_limit = getattr(settings, "SHARE_LIST_MAX_LIMIT", None)
            if not integer_at_least("SHARE_LIST_DEFAULT_LIMIT"):
                errors.append("invalid_share_list_default_limit")
            if not integer_at_least("SHARE_LIST_MAX_LIMIT"):
                errors.append("invalid_share_list_max_limit")
            if (
                integer_at_least("SHARE_LIST_DEFAULT_LIMIT")
                and integer_at_least("SHARE_LIST_MAX_LIMIT")
                and default_limit > max_limit
            ):
                errors.append("invalid_share_list_default_limit")
        return errors

    @staticmethod
    def _configuration_error_applies(
        error: str,
        *,
        share_enabled: bool,
        rag_enabled: bool,
        feature_enabled: bool,
        worker_enabled: bool,
    ) -> bool:
        normalized = str(error).upper()
        rag_only = (
            "RAG_TOP_K",
            "RAG_MAX_TOP_K",
            "RAG_CANDIDATE_LIMIT",
            "RAG_MIN_SCORE",
            "RAG_REFERENCE_MAX_CHARS",
        )
        if any(name in normalized for name in rag_only) and not rag_enabled:
            return False

        worker_only = (
            "SHARE_INDEX_WORKER_POLL_SECONDS",
            "SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS",
        )
        worker_settings_enabled = feature_enabled and worker_enabled
        if any(name in normalized for name in worker_only) and not worker_settings_enabled:
            return False

        service_only = (
            "SHARE_INDEX_LEASE_SECONDS",
            "SHARE_INDEX_MAX_ATTEMPTS",
            "SHARE_INDEX_RETRY_BASE_SECONDS",
            "SHARE_INDEX_RETRY_MAX_SECONDS",
        )
        service_settings_enabled = share_enabled or worker_settings_enabled
        if any(name in normalized for name in service_only) and not service_settings_enabled:
            return False

        share_only = ("SHARE_LIST_DEFAULT_LIMIT", "SHARE_LIST_MAX_LIMIT")
        if any(name in normalized for name in share_only) and not share_enabled:
            return False
        return True

    @staticmethod
    def _factory_accepts_keyword(factory: Callable[..., Any], name: str) -> bool:
        try:
            parameters = inspect.signature(factory).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or (
                parameter.name == name
                and parameter.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            )
            for parameter in parameters
        )

    @staticmethod
    def _configuration_reason(error: str) -> str:
        normalized = str(error).upper()
        for name in (
            "DASHSCOPE_API_KEY",
            "DASHSCOPE_BASE_URL",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSION",
            "EMBEDDING_TIMEOUT_SECONDS",
            "EMBEDDING_MAX_ATTEMPTS",
            "QDRANT_URL",
            "QDRANT_COLLECTION",
            "QDRANT_TIMEOUT_SECONDS",
            "SHARE_LIST_DEFAULT_LIMIT",
            "SHARE_LIST_MAX_LIMIT",
            "RAG_MAX_TOP_K",
            "SHARE_INDEX_LEASE_SECONDS",
            "SHARE_INDEX_MAX_ATTEMPTS",
            "SHARE_INDEX_RETRY_BASE_SECONDS",
            "SHARE_INDEX_RETRY_MAX_SECONDS",
            "SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS",
            "RAG_TOP_K",
            "RAG_CANDIDATE_LIMIT",
            "RAG_MIN_SCORE",
            "RAG_REFERENCE_MAX_CHARS",
            "SHARE_INDEX_WORKER_POLL_SECONDS",
        ):
            if name in normalized:
                return f"invalid_{name.lower()}"
        return "invalid_rag_configuration"

    def _embedding_failure(self, reason: str) -> None:
        self._embedding_failed = True
        self.health.degrade(reason, rag_enabled=self._rag_enabled)

    def _qdrant_failure(self, reason: str) -> None:
        self.health.degrade(reason, qdrant=True, rag_enabled=self._rag_enabled)

    def health_snapshot(self, *, probe: bool = False) -> dict[str, Any]:
        """Return sanitized health; optionally make one lightweight Qdrant call."""

        if probe and self._qdrant_client is not None:
            try:
                self._qdrant_client.get_collections(
                    timeout=self._qdrant_timeout_seconds
                )
            except Exception as error:
                self._qdrant_failure("qdrant_health_check_failed")
                logger.warning(
                    "rag_runtime health=degraded error_class=%s",
                    type(error).__name__,
                )
            else:
                if self._adapters_ready:
                    with self.health._lock:
                        self.health.qdrant = "ready"
                        if not self._embedding_failed:
                            self.health.status = "ready"
                            self.health.rag = (
                                "ready" if self._rag_enabled else "disabled"
                            )
                            self.health.reasons = tuple(
                                reason
                                for reason in self.health.reasons
                                if not reason.startswith("qdrant_")
                            )
        return self.health.snapshot()

    def start(self) -> None:
        with self._lifecycle_lock:
            if (self._started and not self._stopped) or self.worker is None:
                return
            self._started = True
            self._stopped = False
            self.worker.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started or self._stopped or self.worker is None:
                return
            self._stopped = True
            try:
                self.worker.stop()
            finally:
                self._started = False


def create_rag_runtime(**kwargs) -> RagRuntime:
    """Compatibility factory for application wiring and explicit injection."""

    return RagRuntime.from_settings(**kwargs)


__all__ = ["RagHealthState", "RagRuntime", "create_rag_runtime"]
