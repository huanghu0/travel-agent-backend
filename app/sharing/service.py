"""Synchronous publication boundary for public shared trip guides."""

from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable, Literal, Sequence
from uuid import uuid4

from app.observability.rag_metrics import RagMetrics
from app.persistence.exceptions import DraftConflictError
from app.persistence.interfaces import AgentStateStore
from app.rag.embedding import InvalidEmbeddingError
from app.rag.interfaces import EmbeddingClient
from app.rag.qdrant_index import QdrantSharedGuideIndex
from app.rag.text_builder import EmbeddingTextBuilder, _city, _clean, _transport
from app.schemas.trip_schema import TripPlan
from app.services.trip_draft_service import TripDraftService
from app.sharing.exceptions import (
    SharedGuideConflictError,
    SharedGuideForbiddenError,
    SharedGuideUnavailableError,
)
from app.sharing.models import (
    IndexOperation,
    LikeMutation,
    OwnedSharedGuidePage,
    PublicationStatus,
    ShareIndexIntent,
    ShareIndexStatus,
    SharePublishDraft,
    SharedGuideListQuery,
    SharedGuidePage,
    SharedGuidePublicDetail,
    SharedGuideRecord,
    SharedGuideSnapshot,
    SharedTripRequestSnapshot,
    utc_now,
)
from app.sharing.store import SharedGuideStore


logger = logging.getLogger(__name__)


class SharedGuideService:
    """Coordinate durable publication transitions with synchronous indexing."""

    def __init__(
        self,
        *,
        state_store: AgentStateStore,
        trip_draft_service: TripDraftService,
        store: SharedGuideStore,
        text_builder: EmbeddingTextBuilder,
        embedding_client: EmbeddingClient,
        vector_index: QdrantSharedGuideIndex,
        write_enabled: bool,
        lease_seconds: float,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        clock: Callable[[], datetime] = utc_now,
        metrics: RagMetrics | None = None,
    ) -> None:
        if write_enabled:
            if lease_seconds <= 0:
                raise ValueError("lease_seconds must be positive")
            if max_attempts <= 0:
                raise ValueError("max_attempts must be positive")
            if retry_base_seconds <= 0 or retry_max_seconds <= 0:
                raise ValueError("retry delays must be positive")
            if retry_base_seconds > retry_max_seconds:
                raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        self.state_store = state_store
        self.trip_draft_service = trip_draft_service
        self.store = store
        self.text_builder = text_builder
        self.embedding_client = embedding_client
        self.vector_index = vector_index
        self.write_enabled = write_enabled
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.clock = clock
        self.metrics = metrics

    def share_session(
        self,
        session_id: str,
        author_user_id: str,
        *,
        title: str | None = None,
    ) -> SharedGuideRecord:
        self._require_write_enabled()
        author_user_id = self._require_user(author_user_id)
        draft = self._prepare_draft(session_id, author_user_id, title=title)
        intent = self.store.create_publish_intent(draft, now=self.clock())
        return self._publish(intent, author_user_id)

    def update(
        self,
        share_id: str,
        author_user_id: str,
        *,
        title: str | None = None,
    ) -> SharedGuideRecord:
        self._require_write_enabled()
        author_user_id = self._require_user(author_user_id)
        current = self.store.get_owned(share_id, author_user_id)
        draft = self._prepare_draft(
            current.source_session_id,
            author_user_id,
            title=title,
        )
        intent = self.store.stage_update(
            share_id,
            author_user_id,
            draft,
            now=self.clock(),
            allow_active_upsert_supersede=True,
        )
        return self._publish(intent, author_user_id)

    def unpublish(self, share_id: str, author_user_id: str) -> SharedGuideRecord:
        self._require_write_enabled()
        author_user_id = self._require_user(author_user_id)
        with self._publication_metrics("delete"):
            return self._unpublish(share_id, author_user_id)

    def _unpublish(self, share_id: str, author_user_id: str) -> SharedGuideRecord:
        intent = self.store.stage_unpublish(
            share_id,
            author_user_id,
            now=self.clock(),
        )
        if not intent.operation_required:
            return intent.record
        if intent.job is None:
            return self.store.get_owned(share_id, author_user_id)

        worker_id = self._sync_worker_id()
        claimed = self.store.claim_index_job(
            intent.job.job_id,
            worker_id,
            now=self.clock(),
            lease_seconds=self.lease_seconds,
        )
        if claimed is None:
            self._best_effort_supersede(intent.job.job_id, worker_id=None)
            return self.store.get_owned(share_id, author_user_id)

        try:
            self.vector_index.delete(
                intent.record.share_id,
                index_version=intent.record.index_version,
            )
        except Exception as error:
            self._best_effort_failure(intent, claimed.attempt_count, worker_id, error)
            return self.store.get_owned(share_id, author_user_id)

        try:
            completed = self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                intent.record.index_version,
                IndexOperation.DELETE,
                worker_id=worker_id,
                now=self.clock(),
            )
        except Exception:
            return self.store.get_owned(share_id, author_user_id)
        if not completed:
            self._best_effort_supersede(intent.job.job_id, worker_id=worker_id)
        return self.store.get_owned(share_id, author_user_id)

    def list_public(
        self,
        *,
        city: str | None = None,
        travel_days: int | None = None,
        transportation: str | None = None,
        sort: Literal["latest", "popular"] = "latest",
        limit: int = 20,
        cursor: str | None = None,
        viewer_user_id: str | None = None,
    ) -> SharedGuidePage:
        query = self._list_query(
            city=city,
            travel_days=travel_days,
            transportation=transportation,
            sort=sort,
            limit=limit,
            cursor=cursor,
        )
        return self.store.list_public(query, viewer_user_id)

    def get_public(
        self,
        share_id: str,
        *,
        viewer_user_id: str | None = None,
    ) -> SharedGuidePublicDetail:
        return self.store.get_public(share_id, viewer_user_id)

    def list_owned(
        self,
        author_user_id: str,
        *,
        city: str | None = None,
        travel_days: int | None = None,
        transportation: str | None = None,
        sort: Literal["latest", "popular"] = "latest",
        limit: int = 20,
        cursor: str | None = None,
    ) -> OwnedSharedGuidePage:
        author_user_id = self._require_user(author_user_id)
        query = self._list_query(
            city=city,
            travel_days=travel_days,
            transportation=transportation,
            sort=sort,
            limit=limit,
            cursor=cursor,
        )
        return self.store.list_owned(author_user_id, query)

    def like(self, share_id: str, user_id: str) -> LikeMutation:
        self._require_write_enabled()
        user_id = self._require_user(user_id)
        return self.store.put_like(share_id, user_id, now=self.clock())

    def unlike(self, share_id: str, user_id: str) -> LikeMutation:
        self._require_write_enabled()
        user_id = self._require_user(user_id)
        return self.store.delete_like(share_id, user_id)

    def _prepare_draft(
        self,
        session_id: str,
        author_user_id: str,
        *,
        title: str | None,
    ) -> SharePublishDraft:
        state = self.state_store.get_state(session_id, user_id=author_user_id)
        if state.status != "completed":
            raise SharedGuideConflictError("only completed trips can be shared")
        try:
            version = self.trip_draft_service.ensure_original_version(state)
        except (DraftConflictError, AttributeError, TypeError, ValueError) as error:
            raise SharedGuideConflictError("the trip has no complete confirmed version") from error
        if getattr(version, "status", "confirmed") != "confirmed":
            raise SharedGuideConflictError("the latest trip version is not confirmed")
        try:
            plan = TripPlan.model_validate(version.trip_plan).model_copy(deep=True)
            request = state.request
            request_snapshot = SharedTripRequestSnapshot(
                city=request.city,
                travel_days=request.travel_days,
                transportation=request.transportation,
                accommodation=request.accommodation,
                preferences=list(request.preferences),
            )
            snapshot = SharedGuideSnapshot(
                request=request_snapshot,
                trip_plan=plan,
            )
            quality = version.quality_snapshot()
            if not quality.quality_level:
                raise ValueError("confirmed version has no quality level")
        except (AttributeError, TypeError, ValueError) as error:
            raise SharedGuideConflictError(
                "the trip has no complete confirmed snapshot"
            ) from error

        built = self.text_builder.build_document(snapshot)
        normalized_title = self._title(title, built.city_normalized, request.travel_days)
        return SharePublishDraft(
            author_user_id=author_user_id,
            source_session_id=session_id,
            source_version_id=version.version_id,
            source_version_number=version.version_number,
            title=normalized_title,
            city=_clean(request.city, 128),
            city_normalized=built.city_normalized,
            travel_days=request.travel_days,
            transportation=built.transportation_normalized,
            accommodation=_clean(request.accommodation, 128),
            preferences=list(request.preferences),
            snapshot=snapshot,
            retrieval_text=built.text,
            content_hash=built.content_hash,
            quality_level=quality.quality_level,
            quality_score=quality.quality_score,
            embedding_model=str(self.embedding_client.model),
            embedding_dimension=int(self.embedding_client.dimension),
            retrieval_template_version=built.template_version,
        )

    def _publish(
        self,
        intent: ShareIndexIntent,
        author_user_id: str,
    ) -> SharedGuideRecord:
        with self._publication_metrics("upsert"):
            return self._publish_unobserved(intent, author_user_id)

    def _publish_unobserved(
        self,
        intent: ShareIndexIntent,
        author_user_id: str,
    ) -> SharedGuideRecord:
        if not intent.operation_required:
            if self._is_ready(intent.record):
                return intent.record
            raise SharedGuideUnavailableError(
                "shared guide indexing is temporarily unavailable"
            )
        if intent.job is None:
            raise SharedGuideConflictError("the publish operation has no index job")

        worker_id = self._sync_worker_id()
        claimed = self.store.claim_index_job(
            intent.job.job_id,
            worker_id,
            now=self.clock(),
            lease_seconds=self.lease_seconds,
        )
        if claimed is None:
            current = self.store.get_owned(intent.record.share_id, author_user_id)
            if self._same_ready_record(current, intent.record):
                return current
            raise SharedGuideConflictError("the publish operation is already in progress")

        try:
            vector = self._validated_vector(
                self.embedding_client.embed(intent.record.retrieval_text),
                intent.record.embedding_dimension,
            )
            self.vector_index.upsert(
                intent.record.share_id,
                vector,
                payload=self._index_payload(intent.record),
            )
        except Exception as error:
            self._best_effort_failure(intent, claimed.attempt_count, worker_id, error)
            raise SharedGuideUnavailableError(
                "shared guide indexing is temporarily unavailable"
            ) from None

        try:
            completed = self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                intent.record.index_version,
                IndexOperation.UPSERT,
                worker_id=worker_id,
                now=self.clock(),
            )
        except Exception:
            raise SharedGuideUnavailableError(
                "shared guide indexing is temporarily unavailable"
            ) from None
        if not completed:
            self._best_effort_delete(intent.record)
            self._best_effort_supersede(intent.job.job_id, worker_id=worker_id)
            raise SharedGuideConflictError("the publish operation was superseded")
        return self.store.get_owned(intent.record.share_id, author_user_id)

    @contextmanager
    def _publication_metrics(self, stage: str):
        started = time.monotonic()
        outcome = "success"
        try:
            yield
        except SharedGuideConflictError:
            outcome = "conflict"
            raise
        except SharedGuideUnavailableError:
            outcome = "unavailable"
            raise
        except Exception:
            outcome = "failure"
            raise
        finally:
            if self.metrics is not None:
                try:
                    self.metrics.record_publication(
                        stage=stage,
                        outcome=outcome,
                        duration_seconds=max(0.0, time.monotonic() - started),
                    )
                except Exception:
                    logger.debug(
                        "failed to record shared-guide publication metrics",
                        exc_info=True,
                    )

    def _best_effort_failure(
        self,
        intent: ShareIndexIntent,
        attempt_count: int,
        worker_id: str,
        error: BaseException,
    ) -> None:
        if intent.job is None:
            return
        now = self.clock()
        terminal = attempt_count >= self.max_attempts
        next_retry_at = (
            None
            if terminal
            else now + timedelta(seconds=self._retry_delay(attempt_count))
        )
        try:
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                intent.record.index_version,
                intent.job.operation,
                error,
                worker_id=worker_id,
                next_retry_at=next_retry_at,
                terminal=terminal,
                now=now,
            )
        except Exception:
            pass

    def _best_effort_delete(self, record: SharedGuideRecord) -> None:
        try:
            self.vector_index.delete(
                record.share_id,
                index_version=record.index_version,
            )
        except Exception:
            pass

    def _best_effort_supersede(
        self,
        job_id: str,
        *,
        worker_id: str | None,
    ) -> None:
        try:
            self.store.supersede_index_job(
                job_id,
                worker_id=worker_id,
                now=self.clock(),
            )
        except Exception:
            pass

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = max(attempt_count - 1, 0)
        return min(self.retry_max_seconds, self.retry_base_seconds * (2**exponent))

    @staticmethod
    def _validated_vector(
        values: Sequence[float],
        expected_dimension: int,
    ) -> list[float]:
        try:
            vector = [float(value) for value in values]
        except (TypeError, ValueError, OverflowError):
            raise InvalidEmbeddingError("embedding values were not numeric") from None
        if len(vector) != expected_dimension:
            raise InvalidEmbeddingError(
                "embedding dimension did not match persisted configuration"
            )
        if not all(math.isfinite(value) for value in vector):
            raise InvalidEmbeddingError("embedding values were not finite")
        return vector

    @staticmethod
    def _index_payload(record: SharedGuideRecord) -> dict[str, object]:
        if record.published_at is None:
            raise SharedGuideConflictError("staged publish has no publication timestamp")
        return {
            "share_id": record.share_id,
            "city": record.city_normalized,
            "travel_days": record.travel_days,
            "transportation": record.transportation,
            "visibility": "PUBLIC",
            "quality_score": (
                record.quality_score if record.quality_score is not None else 0.0
            ),
            "published_at": int(record.published_at.timestamp()),
            "index_version": record.index_version,
            "content_hash": record.content_hash,
        }

    @staticmethod
    def _same_ready_record(
        current: SharedGuideRecord,
        expected: SharedGuideRecord,
    ) -> bool:
        return (
            current.share_id == expected.share_id
            and current.index_version == expected.index_version
            and current.content_hash == expected.content_hash
            and SharedGuideService._is_ready(current)
        )

    @staticmethod
    def _is_ready(record: SharedGuideRecord) -> bool:
        return (
            record.publication_status is PublicationStatus.PUBLIC
            and record.index_status is ShareIndexStatus.READY
        )

    @staticmethod
    def _title(value: str | None, city_normalized: str, travel_days: int) -> str:
        normalized = _clean(value, 10_000) if value is not None else ""
        if not normalized:
            normalized = f"{city_normalized}{travel_days}日旅行攻略"
        if len(normalized) > 200:
            raise ValueError("title must contain 1 to 200 characters")
        return normalized

    @staticmethod
    def _list_query(
        *,
        city: str | None,
        travel_days: int | None,
        transportation: str | None,
        sort: Literal["latest", "popular"],
        limit: int,
        cursor: str | None,
    ) -> SharedGuideListQuery:
        normalized_city = _city(city) if city is not None else None
        normalized_transport = (
            _transport(transportation) if transportation is not None else None
        )
        return SharedGuideListQuery(
            city_normalized=normalized_city or None,
            travel_days=travel_days,
            transportation=normalized_transport or None,
            sort=sort,
            limit=limit,
            cursor=cursor,
        )

    @staticmethod
    def _sync_worker_id() -> str:
        return f"sync:{uuid4()}"

    @staticmethod
    def _require_user(user_id: str) -> str:
        normalized = str(user_id or "").strip()
        if not normalized:
            raise SharedGuideForbiddenError("authentication is required")
        return normalized

    def _require_write_enabled(self) -> None:
        if not self.write_enabled:
            raise SharedGuideUnavailableError("shared guide writes are unavailable")
