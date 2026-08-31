"""Low-cardinality Prometheus metrics for shared-guide RAG operations."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


logger = logging.getLogger(__name__)


class RagMetrics:
    """Own RAG collectors in a private registry that can be mounted elsewhere."""

    _LABEL_NAMES = {
        "rag_outcomes": ("outcome",),
        "embedding_outcomes": ("outcome",),
        "qdrant_operations": ("operation", "outcome"),
        "share_publications": ("stage", "outcome"),
        "retrieval_stages": ("stage", "outcome"),
        "candidate_counts": ("stage",),
        "index_jobs": ("operation", "outcome"),
        "index_backlog": ("status",),
    }

    def __init__(self) -> None:
        self._registry = CollectorRegistry(auto_describe=True)
        self.rag_outcomes = Counter(
            "travel_agent_rag_requests_total",
            "RAG retrieval outcomes.",
            self._LABEL_NAMES["rag_outcomes"],
            registry=self._registry,
        )
        self.rag_duration = Histogram(
            "travel_agent_rag_duration_seconds",
            "RAG retrieval duration.",
            ("outcome",),
            registry=self._registry,
        )
        self.embedding_outcomes = Counter(
            "travel_agent_embedding_requests_total",
            "Embedding request outcomes.",
            self._LABEL_NAMES["embedding_outcomes"],
            registry=self._registry,
        )
        self.embedding_duration = Histogram(
            "travel_agent_embedding_duration_seconds",
            "Embedding request duration.",
            ("outcome",),
            registry=self._registry,
        )
        self.qdrant_operations = Counter(
            "travel_agent_qdrant_operations_total",
            "Qdrant operation outcomes.",
            self._LABEL_NAMES["qdrant_operations"],
            registry=self._registry,
        )
        self.qdrant_duration = Histogram(
            "travel_agent_qdrant_operation_duration_seconds",
            "Qdrant operation duration.",
            ("operation", "outcome"),
            registry=self._registry,
        )
        self.share_publications = Counter(
            "travel_agent_share_publications_total",
            "Shared-guide publication outcomes.",
            self._LABEL_NAMES["share_publications"],
            registry=self._registry,
        )
        self.share_publication_duration = Histogram(
            "travel_agent_share_publication_duration_seconds",
            "Shared-guide publication duration.",
            ("outcome",),
            registry=self._registry,
        )
        self.retrieval_stages = Counter(
            "travel_agent_rag_retrieval_stages_total",
            "RAG retrieval filter-stage outcomes.",
            self._LABEL_NAMES["retrieval_stages"],
            registry=self._registry,
        )
        self.candidate_counts = Histogram(
            "travel_agent_rag_candidate_count",
            "RAG candidates retained after application checks.",
            self._LABEL_NAMES["candidate_counts"],
            buckets=(0, 1, 2, 3, 5, 10, 20, 50),
            registry=self._registry,
        )
        self.index_jobs = Counter(
            "travel_agent_share_index_jobs_total",
            "Shared-guide index job outcomes.",
            self._LABEL_NAMES["index_jobs"],
            registry=self._registry,
        )
        self.index_backlog = Gauge(
            "travel_agent_share_index_backlog",
            "Current shared-guide index backlog by bounded status.",
            self._LABEL_NAMES["index_backlog"],
            registry=self._registry,
        )
        self.index_oldest_age = Gauge(
            "travel_agent_share_index_oldest_due_age_seconds",
            "Age of the oldest due shared-guide index job.",
            registry=self._registry,
        )

    _RAG_OUTCOMES = frozenset(
        {
            "hit",
            "embedding_unavailable",
            "qdrant_unavailable",
            "mysql_recheck_empty",
            "below_threshold",
            "no_same_city_candidate",
            "invalid_reference",
            "unexpected_error",
        }
    )
    _RETRIEVAL_STAGES = frozenset(
        {
            "exact_days_transport",
            "exact_days",
            "days_plus_minus_one",
            "same_city",
        }
    )
    _PUBLICATION_STAGES = frozenset({"upsert", "delete"})
    _PUBLICATION_OUTCOMES = frozenset(
        {"success", "failure", "conflict", "unavailable"}
    )
    _INDEX_OPERATIONS = frozenset({"UPSERT", "DELETE"})
    _INDEX_OUTCOMES = frozenset({"success", "failure"})

    @staticmethod
    def _enum_value(value: Any) -> str:
        return str(getattr(value, "value", value))

    @staticmethod
    def _bounded(value: Any, allowed: frozenset[str], fallback: str) -> str:
        normalized = RagMetrics._enum_value(value)
        return normalized if normalized in allowed else fallback

    def record_retrieval(
        self,
        *,
        outcome: str,
        duration_seconds: float,
        stage: Any = "unknown",
        candidate_count: int = 0,
    ) -> None:
        """Record one retrieval without allowing telemetry to affect serving."""

        try:
            bounded_outcome = self._bounded(
                outcome,
                self._RAG_OUTCOMES,
                "unexpected_error",
            )
            bounded_stage = self._bounded(
                stage,
                self._RETRIEVAL_STAGES,
                "unknown",
            )
            duration = float(duration_seconds)
            if not math.isfinite(duration) or duration < 0:
                duration = 0.0
            count = max(0, int(candidate_count))
            self.rag_outcomes.labels(outcome=bounded_outcome).inc()
            self.rag_duration.labels(outcome=bounded_outcome).observe(duration)
            self.candidate_counts.labels(stage=bounded_stage).observe(count)
        except Exception:
            logger.debug("failed to record rag retrieval metrics", exc_info=True)

    def record_retrieval_stage(self, *, stage: Any, outcome: str) -> None:
        """Record a bounded outcome for one staged vector search."""

        try:
            bounded_stage = self._bounded(
                stage,
                self._RETRIEVAL_STAGES,
                "unknown",
            )
            bounded_outcome = (
                outcome if outcome in {"candidate", "empty", "failure"} else "failure"
            )
            self.retrieval_stages.labels(
                stage=bounded_stage,
                outcome=bounded_outcome,
            ).inc()
        except Exception:
            logger.debug("failed to record rag stage metrics", exc_info=True)

    def record_publication(
        self,
        *,
        stage: Any,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        """Record one synchronous publication stage with bounded labels."""

        try:
            bounded_stage = self._bounded(
                stage,
                self._PUBLICATION_STAGES,
                "unknown",
            )
            bounded_outcome = self._bounded(
                outcome,
                self._PUBLICATION_OUTCOMES,
                "failure",
            )
            duration = float(duration_seconds)
            if not math.isfinite(duration) or duration < 0:
                duration = 0.0
            self.share_publications.labels(
                stage=bounded_stage,
                outcome=bounded_outcome,
            ).inc()
            self.share_publication_duration.labels(
                outcome=bounded_outcome,
            ).observe(duration)
        except Exception:
            logger.debug("failed to record shared-guide metrics", exc_info=True)

    def record_index_job(self, *, operation: Any, outcome: str) -> None:
        """Record one durable index-job outcome with no job identity labels."""

        try:
            bounded_operation = self._bounded(
                operation,
                self._INDEX_OPERATIONS,
                "unknown",
            )
            bounded_outcome = self._bounded(
                outcome,
                self._INDEX_OUTCOMES,
                "failure",
            )
            self.index_jobs.labels(
                operation=bounded_operation,
                outcome=bounded_outcome,
            ).inc()
        except Exception:
            logger.debug("failed to record shared-guide index metrics", exc_info=True)

    def record_index_backlog(self, *, backlog: Any, now: datetime) -> None:
        """Update bounded backlog gauges after a worker observes the store."""

        try:
            values = {
                "pending": getattr(backlog, "pending_count", 0),
                "running": getattr(backlog, "running_count", 0),
                "failed": getattr(backlog, "failed_count", 0),
                "due": getattr(backlog, "due_count", 0),
            }
            for status, value in values.items():
                self.index_backlog.labels(status=status).set(max(0, int(value)))

            oldest_due_at = getattr(backlog, "oldest_due_at", None)
            age = 0.0
            if oldest_due_at is not None:
                age = max(0.0, (now - oldest_due_at).total_seconds())
                if not math.isfinite(age):
                    age = 0.0
            self.index_oldest_age.set(age)
        except Exception:
            logger.debug("failed to record shared-guide backlog metrics", exc_info=True)

    def collect(self):
        """Allow registration as one collector in the application's registry."""

        yield from self._registry.collect()

    def label_names(self) -> dict[str, tuple[str, ...]]:
        """Expose the bounded label schema for tests and operational review."""

        return dict(self._LABEL_NAMES)


rag_metrics = RagMetrics()


__all__ = ["RagMetrics", "rag_metrics"]
