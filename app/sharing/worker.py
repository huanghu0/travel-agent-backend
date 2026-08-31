"""Durable compensation worker for shared-guide vector index jobs."""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Sequence

from app.observability.rag_metrics import RagMetrics
from app.rag.embedding import InvalidEmbeddingError
from app.rag.qdrant_index import QdrantSharedGuideIndex
from app.sharing.models import (
    IndexOperation,
    PublicationStatus,
    ShareIndexJob,
    ShareIndexStatus,
    SharedGuideRecord,
    utc_now,
)
from app.sharing.store import SharedGuideStore


logger = logging.getLogger(__name__)


class ShareIndexWorker:
    """Claim and compensate durable UPSERT/DELETE jobs one at a time."""

    _MAX_RECONCILIATION_PASSES = 3

    def __init__(
        self,
        *,
        store: SharedGuideStore,
        embedding_client,
        vector_index: QdrantSharedGuideIndex,
        worker_id: str,
        poll_seconds: float,
        lease_seconds: float,
        max_attempts: int,
        retry_base_seconds: float,
        retry_max_seconds: float,
        shutdown_timeout_seconds: float,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        metrics: RagMetrics | None = None,
    ) -> None:
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if poll_seconds <= 0 or lease_seconds <= 0:
            raise ValueError("poll_seconds and lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive")
        if retry_base_seconds > retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self.store = store
        self.embedding_client = embedding_client
        self.vector_index = vector_index
        self.worker_id = worker_id
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.clock = clock
        self.sleep = sleep
        self.metrics = metrics
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._want_running = False
        self._restart_requested = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                if self._stop_event.is_set():
                    self._want_running = True
                    self._restart_requested = True
                return
            self._want_running = True
            self._restart_requested = False
            self._stop_event.clear()
            self._spawn_thread_locked()

    def stop(self) -> None:
        with self._lock:
            self._want_running = False
            self._restart_requested = False
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
        thread.join(timeout=self.shutdown_timeout_seconds)
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def run_once(self) -> bool:
        try:
            job = self.store.claim_next_index_job(
                self.worker_id,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
                max_attempts=self.max_attempts,
            )
            if job is None:
                return False
            self._execute_job(job)
            return True
        finally:
            self._refresh_metrics()

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    executed = self.run_once()
                except Exception:
                    logger.exception("shared-guide index worker loop failed")
                    executed = False
                if not executed and not self._stop_event.is_set():
                    self.sleep(self.poll_seconds)
        finally:
            with self._lock:
                current = threading.current_thread()
                if self._thread is current:
                    self._thread = None
                if self._restart_requested and self._want_running:
                    self._restart_requested = False
                    self._stop_event.clear()
                    self._spawn_thread_locked()

    def _execute_job(self, job: ShareIndexJob) -> None:
        outcome = "success"
        try:
            if job.operation is IndexOperation.UPSERT:
                self._execute_upsert(job)
            else:
                self._execute_delete(job)
        except Exception as error:
            outcome = "failure"
            self._record_failure(job, error)
        finally:
            if self.metrics is not None:
                try:
                    self.metrics.record_index_job(
                        operation=job.operation,
                        outcome=outcome,
                    )
                except Exception:
                    logger.debug(
                        "failed to record shared-guide index job metrics",
                        exc_info=True,
                    )

    def _spawn_thread_locked(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop,
            name=self.worker_id,
            daemon=True,
        )
        self._thread.start()

    def _refresh_metrics(self) -> None:
        if self.metrics is None:
            return
        try:
            now = self.clock()
            backlog = self.store.count_index_backlog(now=now)
            self.metrics.record_index_backlog(backlog=backlog, now=now)
        except Exception:
            logger.debug(
                "failed to refresh shared-guide index backlog metrics",
                exc_info=True,
            )

    def _execute_upsert(self, job: ShareIndexJob) -> None:
        target = self.store.get_index_record(job.share_id)
        if not self._matches_upsert(target, job):
            self._finish_stale_upsert(job, target)
            return
        assert target is not None
        try:
            vector = self._validated_vector(
                self.embedding_client.embed(target.retrieval_text),
                target.embedding_dimension,
            )
            if not self._same_upsert_target(
                self.store.get_index_record(job.share_id),
                target,
                job,
            ):
                self._finish_stale_upsert(job, target)
                return
            self.vector_index.upsert(
                target.share_id,
                vector,
                payload=self._index_payload(target),
            )
        except Exception:
            current = self.store.get_index_record(job.share_id)
            if not self._same_upsert_target(current, target, job):
                self._finish_stale_upsert(job, target)
                return
            raise
        if not self._same_upsert_target(
            self.store.get_index_record(job.share_id),
            target,
            job,
        ):
            self._finish_stale_upsert(job, target)
            return
        completed = self.store.complete_index_operation(
            job.job_id,
            job.share_id,
            job.index_version,
            IndexOperation.UPSERT,
            worker_id=self.worker_id,
            now=self.clock(),
        )
        if not completed:
            self._finish_stale_upsert(job, target)

    def _execute_delete(self, job: ShareIndexJob) -> None:
        target = self.store.get_index_record(job.share_id)
        if not self._matches_delete(target, job):
            self._supersede(job)
            return
        assert target is not None
        if not self._same_delete_target(
            self.store.get_index_record(job.share_id),
            target,
            job,
        ):
            self._supersede(job)
            return
        self.vector_index.delete(job.share_id, index_version=job.index_version)
        if not self._same_delete_target(
            self.store.get_index_record(job.share_id),
            target,
            job,
        ):
            self._supersede(job)
            return
        completed = self.store.complete_index_operation(
            job.job_id,
            job.share_id,
            job.index_version,
            IndexOperation.DELETE,
            worker_id=self.worker_id,
            now=self.clock(),
        )
        if not completed:
            self._supersede(job)

    def _record_failure(self, job: ShareIndexJob, error: BaseException) -> None:
        now = self.clock()
        terminal = job.attempt_count >= self.max_attempts
        next_retry_at = (
            None
            if terminal
            else now + timedelta(seconds=self._retry_delay(job.attempt_count))
        )
        try:
            recorded = self.store.record_index_failure(
                job.job_id,
                job.share_id,
                job.index_version,
                job.operation,
                error,
                worker_id=self.worker_id,
                next_retry_at=next_retry_at,
                terminal=terminal,
                now=now,
            )
            if not recorded:
                self._supersede(job)
        except Exception:
            logger.debug(
                "failed to persist shared-guide index job failure",
                exc_info=True,
            )

    def _supersede(self, job: ShareIndexJob) -> None:
        try:
            self.store.supersede_index_job(
                job.job_id,
                worker_id=self.worker_id,
                now=self.clock(),
            )
        except Exception:
            logger.debug("failed to supersede stale index job", exc_info=True)

    def _finish_stale_upsert(
        self,
        job: ShareIndexJob,
        expected: SharedGuideRecord | None,
    ) -> None:
        repaired = False
        if expected is not None:
            try:
                repaired = self._reconcile_stale_upsert(job, expected)
            except Exception:
                logger.debug(
                    "failed to reconcile stale shared-guide index job",
                    exc_info=True,
                )
        if repaired:
            self._supersede(job)
        else:
            self._record_stale_failure(job)

    def _record_stale_failure(self, job: ShareIndexJob) -> None:
        now = self.clock()
        terminal = job.attempt_count >= self.max_attempts
        next_retry_at = (
            None
            if terminal
            else now + timedelta(seconds=self._retry_delay(job.attempt_count))
        )
        try:
            recorded = self.store.record_index_job_failure(
                job.job_id,
                job.share_id,
                job.index_version,
                job.operation,
                RuntimeError("shared-guide index reconciliation failed"),
                worker_id=self.worker_id,
                next_retry_at=next_retry_at,
                terminal=terminal,
                now=now,
            )
            if not recorded:
                logger.debug("stale index job failure lost its lease")
        except Exception:
            logger.debug(
                "failed to persist stale shared-guide index job failure",
                exc_info=True,
            )

    def _reconcile_stale_upsert(
        self,
        job: ShareIndexJob,
        expected: SharedGuideRecord,
    ) -> bool:
        """Repair a single-point overwrite before abandoning a stale UPSERT.

        A newer PUBLIC/READY record may already have been indexed successfully,
        but a late older writer can still overwrite the same Qdrant point.  In
        that state deletion is unsafe: the version-filtered selector cannot
        distinguish an old payload from the current point id.  Restore the
        exact current record, or demote it and requeue its successful job before
        cleaning the stale point.
        """

        restored_hashes: dict[int, str] = {}
        current = self.store.get_index_record(job.share_id)
        for _ in range(self._MAX_RECONCILIATION_PASSES):
            if current is None:
                return False
            if current.index_version == job.index_version:
                if self._same_ready_record(current, expected):
                    return True
                return self._cleanup_stale_upsert_effects(
                    job,
                    expected=expected,
                    current=current,
                    restored_hashes=restored_hashes,
                )
            if current.index_version < job.index_version:
                return False
            if not self._is_public_ready(current):
                return self._cleanup_stale_upsert_effects(
                    job,
                    expected=expected,
                    current=current,
                    restored_hashes=restored_hashes,
                )

            restored = self._try_restore_ready_record(
                current,
                restored_hashes=restored_hashes,
            )
            if restored is True:
                return True
            if restored is False:
                requeued = self._requeue_ready_record(current)
                latest = self.store.get_index_record(job.share_id)
                if requeued:
                    return self._cleanup_stale_upsert_effects(
                        job,
                        expected=expected,
                        current=latest,
                        restored_hashes=restored_hashes,
                    )
                current = latest
                continue

            current = self.store.get_index_record(job.share_id)

        current = self.store.get_index_record(job.share_id)
        if current is None:
            return False
        if current.index_version == job.index_version:
            return self._same_ready_record(current, expected) or self._cleanup_stale_upsert_effects(
                job,
                expected=expected,
                current=current,
                restored_hashes=restored_hashes,
            )
        if current.index_version < job.index_version:
            return False
        if self._is_public_ready(current):
            return False
        return self._cleanup_stale_upsert_effects(
            job,
            expected=expected,
            current=current,
            restored_hashes=restored_hashes,
        )

    def _try_restore_ready_record(
        self,
        expected: SharedGuideRecord,
        *,
        restored_hashes: dict[int, str],
    ) -> bool | None:
        """Return true for a confirmed restore, false for provider failure."""

        try:
            vector = self._validated_vector(
                self.embedding_client.embed(expected.retrieval_text),
                expected.embedding_dimension,
            )
        except Exception:
            logger.debug("failed to embed current shared-guide index", exc_info=True)
            return False

        current = self.store.get_index_record(expected.share_id)
        if not self._same_ready_record(current, expected):
            return None
        restored_hashes[expected.index_version] = expected.content_hash
        try:
            self.vector_index.upsert(
                expected.share_id,
                vector,
                payload=self._index_payload(expected),
            )
        except Exception:
            logger.debug("failed to restore current shared-guide index", exc_info=True)
            return False
        return self._same_ready_record(
            self.store.get_index_record(expected.share_id),
            expected,
        )

    def _requeue_ready_record(self, record: SharedGuideRecord) -> bool:
        try:
            return self.store.requeue_current_upsert(
                record.share_id,
                record.index_version,
                record.content_hash,
                now=self.clock(),
            )
        except Exception:
            logger.debug(
                "failed to requeue current shared-guide index",
                exc_info=True,
            )
            return False

    def _cleanup_upsert(
        self,
        job: ShareIndexJob,
        *,
        expected: SharedGuideRecord,
        current: SharedGuideRecord | None = None,
    ) -> bool:
        if current is None:
            current = self.store.get_index_record(job.share_id)
        if not self._cleanup_is_safe(current, job, expected):
            return False
        try:
            self.vector_index.delete(
                job.share_id,
                index_version=job.index_version,
            )
        except Exception:
            logger.debug("failed to clean stale index point", exc_info=True)
            return False
        return True

    def _cleanup_stale_upsert_effects(
        self,
        job: ShareIndexJob,
        *,
        expected: SharedGuideRecord,
        current: SharedGuideRecord | None,
        restored_hashes: dict[int, str],
    ) -> bool:
        if not self._cleanup_upsert(job, expected=expected, current=current):
            return False
        latest = self.store.get_index_record(job.share_id)
        if latest is None:
            return False
        cleanup_hashes = dict(restored_hashes)
        if (
            latest.publication_status is PublicationStatus.UNPUBLISHED
            and latest.index_status
            in (
                ShareIndexStatus.DELETE_PENDING,
                ShareIndexStatus.DELETED,
                ShareIndexStatus.FAILED,
            )
        ):
            cleanup_hashes.setdefault(latest.index_version, latest.content_hash)
        for index_version, content_hash in sorted(cleanup_hashes.items()):
            if index_version == job.index_version:
                continue
            if not self._cleanup_unpublished_version(
                share_id=job.share_id,
                index_version=index_version,
                content_hash=content_hash,
                current=latest,
            ):
                return False
        return True

    def _cleanup_unpublished_version(
        self,
        *,
        share_id: str,
        index_version: int,
        content_hash: str,
        current: SharedGuideRecord,
    ) -> bool:
        if not (
            current.share_id == share_id
            and current.index_version == index_version
            and current.publication_status is PublicationStatus.UNPUBLISHED
            and current.index_status
            in (
                ShareIndexStatus.DELETE_PENDING,
                ShareIndexStatus.DELETED,
                ShareIndexStatus.FAILED,
            )
            and current.content_hash == content_hash
        ):
            return False
        try:
            self.vector_index.delete(share_id, index_version=index_version)
        except Exception:
            logger.debug(
                "failed to clean restored unpublished index point",
                exc_info=True,
            )
            return False
        return True

    @staticmethod
    def _cleanup_is_safe(
        current: SharedGuideRecord | None,
        job: ShareIndexJob,
        expected: SharedGuideRecord,
    ) -> bool:
        if (
            current is None
            or current.share_id != job.share_id
            or expected.share_id != job.share_id
        ):
            return False
        if current.index_version > job.index_version:
            return (
                current.publication_status
                in (PublicationStatus.PUBLISHING, PublicationStatus.UNPUBLISHED)
                and current.index_status
                in (
                    ShareIndexStatus.PENDING,
                    ShareIndexStatus.FAILED,
                    ShareIndexStatus.DELETE_PENDING,
                    ShareIndexStatus.DELETED,
                )
            )
        if current.index_version != job.index_version:
            return False
        return bool(
            expected.index_version == job.index_version
            and current.publication_status is PublicationStatus.UNPUBLISHED
            and current.index_status
            in (
                ShareIndexStatus.DELETE_PENDING,
                ShareIndexStatus.DELETED,
                ShareIndexStatus.FAILED,
            )
            and current.content_hash == expected.content_hash
        )

    @staticmethod
    def _is_public_ready(record: SharedGuideRecord | None) -> bool:
        return bool(
            record
            and record.publication_status is PublicationStatus.PUBLIC
            and record.index_status is ShareIndexStatus.READY
        )

    @classmethod
    def _same_ready_record(
        cls,
        current: SharedGuideRecord | None,
        expected: SharedGuideRecord,
    ) -> bool:
        return bool(
            cls._is_public_ready(current)
            and current
            and current.share_id == expected.share_id
            and current.index_version == expected.index_version
            and current.content_hash == expected.content_hash
        )

    def _retry_delay(self, attempt_count: int) -> float:
        return min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(attempt_count - 1, 0)),
        )

    @staticmethod
    def _matches_upsert(
        record: SharedGuideRecord | None,
        job: ShareIndexJob,
    ) -> bool:
        return bool(
            record
            and record.index_version == job.index_version
            and record.publication_status is PublicationStatus.PUBLISHING
            and record.index_status
            in (ShareIndexStatus.PENDING, ShareIndexStatus.FAILED)
        )

    @classmethod
    def _same_upsert_target(
        cls,
        current: SharedGuideRecord | None,
        expected: SharedGuideRecord,
        job: ShareIndexJob,
    ) -> bool:
        return bool(
            cls._matches_upsert(current, job)
            and current
            and current.content_hash == expected.content_hash
        )

    @staticmethod
    def _matches_delete(
        record: SharedGuideRecord | None,
        job: ShareIndexJob,
    ) -> bool:
        return bool(
            record
            and record.index_version == job.index_version
            and record.publication_status is PublicationStatus.UNPUBLISHED
            and record.index_status
            in (ShareIndexStatus.DELETE_PENDING, ShareIndexStatus.FAILED)
        )

    @classmethod
    def _same_delete_target(
        cls,
        current: SharedGuideRecord | None,
        expected: SharedGuideRecord,
        job: ShareIndexJob,
    ) -> bool:
        return bool(
            cls._matches_delete(current, job)
            and current
            and current.content_hash == expected.content_hash
        )

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
            raise ValueError("staged publish has no publication timestamp")
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
