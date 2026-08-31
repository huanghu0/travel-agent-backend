"""Durable shared-guide index worker tests using file-backed SQLite."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from sqlalchemy import create_engine, select, update

from app.observability.rag_metrics import RagMetrics
from app.persistence.sqlalchemy_models import (
    Base,
    ShareIndexJobRow,
    SharedGuideRow,
    UserRow,
)
from app.sharing.models import (
    IndexJobStatus,
    IndexOperation,
    PublicationStatus,
    ShareIndexStatus,
)
from app.sharing.mysql_store import MySQLSharedGuideStore
from tests.test_shared_guide_store import make_draft


UTC = timezone.utc
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def metric_sample_value(metrics: RagMetrics, name: str, labels: dict[str, str] | None = None):
    labels = labels or {}
    for family in metrics.collect():
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None


class MutableClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeEmbeddingClient:
    model = "qwen3.7-text-embedding"
    dimension = 768

    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("provider secret must not be persisted")
        return [0.01] * self.dimension


class FakeVectorIndex:
    """Models Qdrant's version-filtered delete semantics in memory."""

    def __init__(self) -> None:
        self.points: dict[str, dict[str, object]] = {}
        self.upserts: list[tuple[str, int]] = []
        self.deletes: list[tuple[str, int]] = []
        self.delete_failures = 0
        self.delete_failure_versions: set[int] = set()
        self.before_upsert: Callable[[str, dict[str, object]], None] | None = None
        self.before_delete: Callable[[str, int], None] | None = None

    def upsert(
        self,
        share_id: str,
        vector: list[float],
        *,
        payload: dict[str, object],
    ) -> None:
        if self.before_upsert is not None:
            self.before_upsert(share_id, payload)
        version = int(payload["index_version"])
        self.upserts.append((share_id, version))
        self.points[share_id] = dict(payload)

    def delete(self, share_id: str, *, index_version: int) -> None:
        if self.before_delete is not None:
            self.before_delete(share_id, index_version)
        if self.delete_failures > 0:
            self.delete_failures -= 1
            raise RuntimeError("vector delete failed")
        if index_version in self.delete_failure_versions:
            raise RuntimeError("vector delete failed")
        self.deletes.append((share_id, index_version))
        current = self.points.get(share_id)
        if current is not None and current["index_version"] == index_version:
            del self.points[share_id]


class ClaimHookStore:
    """Runs a state transition immediately after the worker obtains its lease."""

    def __init__(self, store: MySQLSharedGuideStore, hook: Callable[[], None]) -> None:
        self._store = store
        self._hook = hook

    def claim_next_index_job(self, *args, **kwargs):
        claimed = self._store.claim_next_index_job(*args, **kwargs)
        if claimed is not None:
            hook, self._hook = self._hook, lambda: None
            hook()
        return claimed

    def __getattr__(self, name: str):
        return getattr(self._store, name)


class ShareIndexWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = Path(self.directory.name) / "sharing-worker.db"
        self.engine = create_engine(
            f"sqlite:///{database}",
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        Base.metadata.create_all(self.engine)
        self.store = MySQLSharedGuideStore(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                UserRow.__table__.insert().values(
                    user_id="user-1",
                    username="alice",
                    password_hash="not-used",
                    created_at=NOW.replace(tzinfo=None),
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    def _intent(self, marker: str, *, now: datetime, version: int = 1):
        return self.store.create_publish_intent(
            make_draft(
                source_session_id=f"session-{marker}",
                source_version_number=version,
                content_marker=marker,
            ),
            now=now,
        )

    def _worker(
        self,
        *,
        store=None,
        embedding: FakeEmbeddingClient | None = None,
        vector: FakeVectorIndex | None = None,
        worker_id: str = "worker-A",
        clock: MutableClock | None = None,
        sleep: Callable[[float], None] = lambda _: None,
        max_attempts: int = 3,
        retry_base_seconds: float = 2,
        retry_max_seconds: float = 3,
        shutdown_timeout_seconds: float = 0.05,
        metrics: RagMetrics | None = None,
    ):
        from app.sharing.worker import ShareIndexWorker

        worker_kwargs = {
            "store": store or self.store,
            "embedding_client": embedding or FakeEmbeddingClient(),
            "vector_index": vector or FakeVectorIndex(),
            "worker_id": worker_id,
            "poll_seconds": 0.01,
            "lease_seconds": 30,
            "max_attempts": max_attempts,
            "retry_base_seconds": retry_base_seconds,
            "retry_max_seconds": retry_max_seconds,
            "shutdown_timeout_seconds": shutdown_timeout_seconds,
            "clock": clock or MutableClock(),
            "sleep": sleep,
        }
        if metrics is not None:
            worker_kwargs["metrics"] = metrics
        return ShareIndexWorker(**worker_kwargs)

    def _job_row(self, job_id: str):
        with self.engine.connect() as connection:
            return connection.execute(
                select(ShareIndexJobRow.__table__).where(
                    ShareIndexJobRow.job_id == job_id
                )
            ).mappings().one()

    def _reclaimed_old_upsert_after_write(self):
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        clock = MutableClock()

        def expire_after_write(_share_id: str, _payload: dict[str, object]) -> None:
            clock.now = NOW + timedelta(seconds=31)

        vector.before_upsert = expire_after_write
        self.assertTrue(self._worker(vector=vector, clock=clock).run_once())
        self.assertEqual(vector.points[intent.record.share_id]["index_version"], 1)

        newer = self.store.stage_update(
            intent.record.share_id,
            "user-1",
            make_draft(
                source_session_id="session-a",
                source_version_number=2,
                content_marker="b",
            ),
            now=NOW + timedelta(seconds=32),
            allow_active_upsert_supersede=True,
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(ShareIndexJobRow.__table__)
                .where(ShareIndexJobRow.job_id == intent.job.job_id)
                .values(
                    status=IndexJobStatus.PENDING.value,
                    attempt_count=1,
                    next_retry_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
        vector.before_upsert = None
        clock.now = NOW + timedelta(seconds=32)
        return intent, newer, clock, vector

    def _ready_successor_with_stale_old_point(self):
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        self.assertTrue(self._worker(vector=vector).run_once())
        newer = self.store.stage_update(
            intent.record.share_id,
            "user-1",
            make_draft(
                source_session_id="session-a",
                source_version_number=2,
                content_marker="b",
            ),
            now=NOW + timedelta(seconds=1),
            allow_active_upsert_supersede=True,
        )
        self.assertTrue(
            self._worker(
                vector=vector,
                clock=MutableClock(NOW + timedelta(seconds=1)),
            ).run_once()
        )
        vector.points[intent.record.share_id]["index_version"] = 1
        vector.points[intent.record.share_id]["content_hash"] = intent.record.content_hash
        with self.engine.begin() as connection:
            connection.execute(
                update(ShareIndexJobRow.__table__)
                .where(ShareIndexJobRow.job_id == intent.job.job_id)
                .values(
                    status=IndexJobStatus.PENDING.value,
                    attempt_count=1,
                    next_retry_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
        return intent, newer, vector

    def test_claims_only_due_work_in_oldest_order_and_counts_backlog(self) -> None:
        future = self._intent("a", now=NOW)
        oldest_due = self._intent("b", now=NOW + timedelta(seconds=1))
        newer_due = self._intent("c", now=NOW + timedelta(seconds=2))
        active = self._intent("d", now=NOW + timedelta(seconds=3))
        expired = self._intent("e", now=NOW + timedelta(seconds=4))
        with self.engine.begin() as connection:
            connection.execute(
                update(ShareIndexJobRow.__table__)
                .where(ShareIndexJobRow.job_id == future.job.job_id)
                .values(next_retry_at=(NOW + timedelta(seconds=20)).replace(tzinfo=None))
            )
        self.store.claim_index_job(
            active.job.job_id,
            "active-owner",
            now=NOW + timedelta(seconds=3),
            lease_seconds=30,
        )
        self.store.claim_index_job(
            expired.job.job_id,
            "expired-owner",
            now=NOW + timedelta(seconds=4),
            lease_seconds=1,
        )

        backlog = self.store.count_index_backlog(now=NOW + timedelta(seconds=10))
        self.assertEqual(backlog.pending_count, 3)
        self.assertEqual(backlog.running_count, 2)
        self.assertEqual(backlog.failed_count, 0)
        self.assertEqual(backlog.due_count, 3)
        self.assertEqual(backlog.oldest_due_at, NOW + timedelta(seconds=1))

        claimed = self.store.claim_next_index_job(
            "worker-A",
            now=NOW + timedelta(seconds=10),
            lease_seconds=30,
            max_attempts=3,
        )
        self.assertEqual(claimed.job_id, oldest_due.job.job_id)
        self.assertEqual(claimed.status, IndexJobStatus.RUNNING)
        self.assertEqual(claimed.attempt_count, 1)
        self.assertEqual(claimed.lease_owner, "worker-A")
        self.assertEqual(
            claimed.lease_expires_at,
            NOW + timedelta(seconds=40),
        )
        self.assertNotEqual(claimed.job_id, newer_due.job.job_id)

        claimed_newer = self.store.claim_next_index_job(
            "worker-B",
            now=NOW + timedelta(seconds=10),
            lease_seconds=30,
            max_attempts=3,
        )
        self.assertIsNotNone(claimed_newer)
        self.assertEqual(claimed_newer.job_id, newer_due.job.job_id)

        reclaimed_expired = self.store.claim_next_index_job(
            "worker-C",
            now=NOW + timedelta(seconds=10),
            lease_seconds=30,
            max_attempts=3,
        )
        self.assertIsNotNone(reclaimed_expired)
        self.assertEqual(reclaimed_expired.job_id, expired.job.job_id)
        self.assertEqual(reclaimed_expired.attempt_count, 2)
        self.assertEqual(self._job_row(future.job.job_id)["status"], "PENDING")
        self.assertEqual(self._job_row(active.job.job_id)["status"], "RUNNING")

    def test_metrics_record_index_job_outcome_backlog_and_oldest_age(self) -> None:
        metrics = RagMetrics()
        self._intent("m", now=NOW)
        worker = self._worker(metrics=metrics)

        self.assertTrue(worker.run_once())

        self.assertEqual(
            1.0,
            metric_sample_value(
                metrics,
                "travel_agent_share_index_jobs_total",
                {"operation": "UPSERT", "outcome": "success"},
            ),
        )
        for status in ("pending", "running", "failed", "due"):
            self.assertEqual(
                0.0,
                metric_sample_value(
                    metrics,
                    "travel_agent_share_index_backlog",
                    {"status": status},
                ),
            )
        self.assertEqual(
            0.0,
            metric_sample_value(
                metrics,
                "travel_agent_share_index_oldest_due_age_seconds",
            ),
        )

        failure_metrics = RagMetrics()
        self._intent("n", now=NOW)
        failure_worker = self._worker(
            metrics=failure_metrics,
            embedding=FakeEmbeddingClient(failures=1),
        )

        self.assertTrue(failure_worker.run_once())
        self.assertEqual(
            1.0,
            metric_sample_value(
                failure_metrics,
                "travel_agent_share_index_jobs_total",
                {"operation": "UPSERT", "outcome": "failure"},
            ),
        )

    def test_two_workers_cannot_claim_the_same_job(self) -> None:
        intent = self._intent("a", now=NOW)
        barrier = threading.Barrier(2)

        def claim(worker_id: str):
            barrier.wait(timeout=2)
            return self.store.claim_next_index_job(
                worker_id,
                now=NOW,
                lease_seconds=30,
                max_attempts=3,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ("Worker", "worker")))

        claimed = [result for result in results if result is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].job_id, intent.job.job_id)
        self.assertIn(claimed[0].lease_owner, {"Worker", "worker"})

    def test_claim_owner_remains_case_sensitive_at_completion(self) -> None:
        intent = self._intent("a", now=NOW)
        claimed = self.store.claim_next_index_job(
            "Worker",
            now=NOW,
            lease_seconds=30,
            max_attempts=3,
        )
        self.assertIsNotNone(claimed)

        self.assertFalse(
            self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                intent.record.index_version,
                IndexOperation.UPSERT,
                worker_id="worker",
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertTrue(
            self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                intent.record.index_version,
                IndexOperation.UPSERT,
                worker_id="Worker",
                now=NOW + timedelta(seconds=1),
            )
        )

    def test_expired_final_attempt_is_terminalized_without_reclaim(self) -> None:
        intent = self._intent("a", now=NOW)
        with self.engine.begin() as connection:
            connection.execute(
                update(ShareIndexJobRow.__table__)
                .where(ShareIndexJobRow.job_id == intent.job.job_id)
                .values(
                    status=IndexJobStatus.RUNNING.value,
                    attempt_count=3,
                    lease_owner="dead-worker",
                    lease_expires_at=(NOW + timedelta(seconds=1)).replace(tzinfo=None),
                )
            )

        claimed = self.store.claim_next_index_job(
            "worker-A",
            now=NOW + timedelta(seconds=1),
            lease_seconds=30,
            max_attempts=3,
        )

        self.assertIsNone(claimed)
        self.assertEqual(self._job_row(intent.job.job_id)["status"], "FAILED")
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(record.index_status, ShareIndexStatus.FAILED)
        backlog = self.store.count_index_backlog(now=NOW + timedelta(seconds=1))
        self.assertEqual(backlog.due_count, 0)
        self.assertEqual(backlog.failed_count, 1)

    def test_saturated_pending_row_does_not_block_newer_due_work(self) -> None:
        saturated = self._intent("a", now=NOW)
        newer = self._intent("b", now=NOW + timedelta(seconds=1))
        with self.engine.begin() as connection:
            connection.execute(
                update(ShareIndexJobRow.__table__)
                .where(ShareIndexJobRow.job_id == saturated.job.job_id)
                .values(attempt_count=3)
            )

        claimed = self.store.claim_next_index_job(
            "worker-A",
            now=NOW + timedelta(seconds=2),
            lease_seconds=30,
            max_attempts=3,
        )

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.job_id, newer.job.job_id)

    def test_mysql_claim_restarts_when_exact_global_candidate_becomes_ineligible(self) -> None:
        intent = self._intent("a", now=NOW)
        self.store.claim_index_job(
            intent.job.job_id,
            "old-worker",
            now=NOW,
            lease_seconds=30,
        )
        newer_intent = self.store.stage_update(
            intent.record.share_id,
            "user-1",
            make_draft(
                source_session_id="session-a",
                source_version_number=2,
                content_marker="b",
            ),
            now=NOW + timedelta(seconds=1),
            allow_active_upsert_supersede=True,
        )
        selected_ids: list[str] = []
        original_candidate = MySQLSharedGuideStore._oldest_claim_candidate
        original_share_lock = MySQLSharedGuideStore._select_share_for_update
        raced = False

        def capture_candidate(
            _store,
            connection,
            now,
            max_attempts,
            excluded_job_ids=(),
        ):
            candidate = original_candidate(
                connection,
                now,
                max_attempts,
                excluded_job_ids=excluded_job_ids,
            )
            if candidate is not None:
                selected_ids.append(candidate["job_id"])
            return candidate

        def make_exact_job_ineligible(
            _store,
            connection,
            share_id,
            author_user_id=None,
            **kwargs,
        ):
            nonlocal raced
            share_row = original_share_lock(
                connection,
                share_id,
                author_user_id,
                **kwargs,
            )
            if not raced and share_id == intent.record.share_id:
                raced = True
                connection.execute(
                    update(ShareIndexJobRow.__table__)
                    .where(ShareIndexJobRow.job_id == intent.job.job_id)
                    .values(
                        status=IndexJobStatus.RUNNING.value,
                        attempt_count=2,
                        lease_owner="racer",
                        lease_expires_at=(NOW + timedelta(seconds=61)).replace(
                            tzinfo=None
                        ),
                    )
                )
            return share_row

        original_dialect = self.engine.dialect.name
        self.engine.dialect.name = "mysql"
        try:
            with (
                patch.object(
                    MySQLSharedGuideStore,
                    "_oldest_claim_candidate",
                    new=capture_candidate,
                ),
                patch.object(
                    MySQLSharedGuideStore,
                    "_select_share_for_update",
                    new=make_exact_job_ineligible,
                ),
            ):
                claimed = self.store.claim_next_index_job(
                    "worker-A",
                    now=NOW + timedelta(seconds=31),
                    lease_seconds=30,
                    max_attempts=3,
                )
        finally:
            self.engine.dialect.name = original_dialect

        self.assertIsNotNone(claimed)
        self.assertTrue(raced)
        self.assertEqual(claimed.job_id, newer_intent.job.job_id)
        self.assertEqual(selected_ids, [intent.job.job_id, newer_intent.job.job_id])

    def test_mysql_claim_selects_candidate_and_locks_share_in_one_transaction(self) -> None:
        intent = self._intent("a", now=NOW)
        candidate_connections: list[object] = []
        share_connections: list[object] = []
        original_candidate = MySQLSharedGuideStore._oldest_claim_candidate
        original_share_lock = MySQLSharedGuideStore._select_share_for_update

        def capture_candidate(
            _store,
            connection,
            now,
            max_attempts,
            excluded_job_ids=(),
        ):
            candidate_connections.append(connection)
            return original_candidate(
                connection,
                now,
                max_attempts,
                excluded_job_ids=excluded_job_ids,
            )

        def capture_share_lock(
            _store,
            connection,
            share_id,
            author_user_id=None,
            **kwargs,
        ):
            share_connections.append(connection)
            return original_share_lock(
                connection,
                share_id,
                author_user_id,
                **kwargs,
            )

        original_dialect = self.engine.dialect.name
        self.engine.dialect.name = "mysql"
        try:
            with (
                patch.object(
                    MySQLSharedGuideStore,
                    "_oldest_claim_candidate",
                    new=capture_candidate,
                ),
                patch.object(
                    MySQLSharedGuideStore,
                    "_select_share_for_update",
                    new=capture_share_lock,
                ),
            ):
                claimed = self.store.claim_next_index_job(
                    "worker-A",
                    now=NOW,
                    lease_seconds=30,
                    max_attempts=3,
                )
        finally:
            self.engine.dialect.name = original_dialect

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.job_id, intent.job.job_id)
        self.assertEqual(len(candidate_connections), 1)
        self.assertEqual(len(share_connections), 1)
        self.assertIs(candidate_connections[0], share_connections[0])

    def test_mysql_claim_skips_locked_oldest_share_for_newer_due_work(self) -> None:
        oldest = self._intent("a", now=NOW)
        newer = self._intent("b", now=NOW + timedelta(seconds=1))
        original_share_lock = MySQLSharedGuideStore._select_share_for_update
        skipped_oldest = False

        def skip_oldest_once(
            _store,
            connection,
            share_id,
            author_user_id=None,
            **kwargs,
        ):
            nonlocal skipped_oldest
            if share_id == oldest.record.share_id and not skipped_oldest:
                skipped_oldest = True
                return None
            return original_share_lock(
                connection,
                share_id,
                author_user_id,
                **kwargs,
            )

        original_dialect = self.engine.dialect.name
        self.engine.dialect.name = "mysql"
        try:
            with patch.object(
                MySQLSharedGuideStore,
                "_select_share_for_update",
                new=skip_oldest_once,
            ):
                claimed = self.store.claim_next_index_job(
                    "worker-A",
                    now=NOW + timedelta(seconds=1),
                    lease_seconds=30,
                    max_attempts=3,
                )
        finally:
            self.engine.dialect.name = original_dialect

        self.assertTrue(skipped_oldest)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.job_id, newer.job.job_id)
        self.assertEqual(self._job_row(oldest.job.job_id)["status"], "PENDING")

    def test_worker_rechecks_version_and_publication_before_embedding(self) -> None:
        for transition, marker in (("update", "u"), ("unpublish", "p")):
            with self.subTest(transition=transition):
                intent = self._intent(marker, now=NOW)
                embedding = FakeEmbeddingClient()
                if transition == "update":
                    hook = lambda: self.store.stage_update(
                        intent.record.share_id,
                        "user-1",
                        make_draft(
                            source_session_id=f"session-{marker}",
                            source_version_number=2,
                            content_marker="z",
                        ),
                        now=NOW + timedelta(seconds=1),
                        allow_active_upsert_supersede=True,
                    )
                else:
                    hook = lambda: self.store.stage_unpublish(
                        intent.record.share_id,
                        "user-1",
                        now=NOW + timedelta(seconds=1),
                    )
                worker = self._worker(
                    store=ClaimHookStore(self.store, hook),
                    embedding=embedding,
                )

                self.assertTrue(worker.run_once())
                self.assertEqual(embedding.calls, [])

    def test_old_upsert_cannot_survive_a_newer_out_of_order_update(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        triggered = False

        def publish_newer(_share_id: str, payload: dict[str, object]) -> None:
            nonlocal triggered
            if triggered or payload["index_version"] != 1:
                return
            triggered = True
            self.store.stage_update(
                intent.record.share_id,
                "user-1",
                make_draft(
                    source_session_id="session-a",
                    source_version_number=2,
                    content_marker="b",
                ),
                now=NOW + timedelta(seconds=1),
                allow_active_upsert_supersede=True,
            )
            self.assertTrue(
                self._worker(
                    vector=vector,
                    worker_id="worker-new",
                    clock=MutableClock(NOW + timedelta(seconds=1)),
                ).run_once()
            )

        vector.before_upsert = publish_newer
        self.assertTrue(self._worker(vector=vector).run_once())

        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.index_version, 2)
        self.assertEqual(record.publication_status, PublicationStatus.PUBLIC)
        point = vector.points.get(intent.record.share_id)
        self.assertIsNotNone(point)
        self.assertEqual(point["index_version"], 2)
        self.assertEqual(point["content_hash"], record.content_hash)

    def test_ready_successor_is_requeued_when_stale_write_cannot_restore_it(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        embedding = FakeEmbeddingClient()
        triggered = False
        newer_intent = None

        def fail_on_third_embedding(text: str) -> list[float]:
            if len(embedding.calls) == 2:
                embedding.calls.append(text)
                raise RuntimeError("restore provider failure")
            return FakeEmbeddingClient.embed(embedding, text)

        embedding.embed = fail_on_third_embedding  # type: ignore[method-assign]
        worker_b = self._worker(
            vector=vector,
            worker_id="worker-new",
            clock=MutableClock(NOW + timedelta(seconds=1)),
            embedding=embedding,
        )

        def publish_newer(_share_id: str, payload: dict[str, object]) -> None:
            nonlocal triggered, newer_intent
            if triggered or payload["index_version"] != 1:
                return
            triggered = True
            newer_intent = self.store.stage_update(
                intent.record.share_id,
                "user-1",
                make_draft(
                    source_session_id="session-a",
                    source_version_number=2,
                    content_marker="b",
                ),
                now=NOW + timedelta(seconds=1),
                allow_active_upsert_supersede=True,
            )
            self.assertTrue(worker_b.run_once())

        vector.before_upsert = publish_newer
        worker_a = self._worker(vector=vector, embedding=embedding)

        self.assertTrue(worker_a.run_once())
        self.assertIsNotNone(newer_intent)
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(record.index_status, ShareIndexStatus.PENDING)
        self.assertEqual(self._job_row(newer_intent.job.job_id)["status"], "PENDING")
        self.assertNotIn(intent.record.share_id, vector.points)

    def test_reclaimed_stale_upsert_cleans_prior_write_before_superseding(self) -> None:
        intent, newer, clock, vector = self._reclaimed_old_upsert_after_write()

        self.assertTrue(self._worker(vector=vector, clock=clock).run_once())

        self.assertEqual(self._job_row(intent.job.job_id)["status"], "SUCCEEDED")
        self.assertEqual(self._job_row(newer.job.job_id)["status"], "PENDING")
        self.assertNotIn(intent.record.share_id, vector.points)

    def test_cleanup_failure_keeps_reclaimed_stale_upsert_retryable(self) -> None:
        intent, newer, clock, vector = self._reclaimed_old_upsert_after_write()
        vector.delete_failures = 1

        self.assertTrue(self._worker(vector=vector, clock=clock).run_once())

        old_job = self._job_row(intent.job.job_id)
        self.assertEqual(old_job["status"], "PENDING")
        self.assertEqual(old_job["last_error"], "RuntimeError")
        self.assertEqual(self._job_row(newer.job.job_id)["status"], "PENDING")
        self.assertEqual(vector.points[intent.record.share_id]["index_version"], 1)

    def test_restore_and_requeue_failure_never_supersedes_stale_upsert(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        embedding = FakeEmbeddingClient()
        newer_intent = None
        restore_text = None
        restore_calls = 0

        def fail_restore_after_successful_successor(text: str) -> list[float]:
            nonlocal restore_calls
            embedding.calls.append(text)
            if restore_text is not None and text == restore_text:
                restore_calls += 1
                if restore_calls >= 2:
                    raise RuntimeError("restore provider failed")
            return [0.01] * embedding.dimension

        embedding.embed = fail_restore_after_successful_successor  # type: ignore[method-assign]
        worker_b = self._worker(
            vector=vector,
            embedding=embedding,
            worker_id="worker-new",
            clock=MutableClock(NOW + timedelta(seconds=1)),
        )
        triggered = False

        def publish_newer(_share_id: str, payload: dict[str, object]) -> None:
            nonlocal triggered, newer_intent, restore_text
            if triggered or payload["index_version"] != 1:
                return
            triggered = True
            newer_intent = self.store.stage_update(
                intent.record.share_id,
                "user-1",
                make_draft(
                    source_session_id="session-a",
                    source_version_number=2,
                    content_marker="b",
                ),
                now=NOW + timedelta(seconds=1),
                allow_active_upsert_supersede=True,
            )
            restore_text = newer_intent.record.retrieval_text
            self.assertTrue(worker_b.run_once())

        vector.before_upsert = publish_newer
        worker_a = self._worker(vector=vector, embedding=embedding)
        with patch.object(self.store, "requeue_current_upsert", return_value=False):
            self.assertTrue(worker_a.run_once())

        self.assertIsNotNone(newer_intent)
        old_job = self._job_row(intent.job.job_id)
        self.assertEqual(old_job["status"], "PENDING")
        self.assertEqual(old_job["last_error"], "RuntimeError")
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.index_version, 2)
        self.assertEqual(record.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(record.index_status, ShareIndexStatus.READY)
        self.assertEqual(vector.points[intent.record.share_id]["index_version"], 1)

    def test_upsert_error_after_stale_write_repairs_before_superseding(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        newer_intent = None
        triggered = False
        failed_once = True
        original_upsert = vector.upsert

        def publish_newer(_share_id: str, payload: dict[str, object]) -> None:
            nonlocal triggered, newer_intent
            if triggered or payload["index_version"] != 1:
                return
            triggered = True
            newer_intent = self.store.stage_update(
                intent.record.share_id,
                "user-1",
                make_draft(
                    source_session_id="session-a",
                    source_version_number=2,
                    content_marker="b",
                ),
                now=NOW + timedelta(seconds=1),
                allow_active_upsert_supersede=True,
            )
            self.assertTrue(
                self._worker(
                    vector=vector,
                    worker_id="worker-new",
                    clock=MutableClock(NOW + timedelta(seconds=1)),
                ).run_once()
            )

        def write_then_fail(
            share_id: str,
            values: list[float],
            *,
            payload: dict[str, object],
        ) -> None:
            nonlocal failed_once
            original_upsert(share_id, values, payload=payload)
            if failed_once and payload["index_version"] == 1:
                failed_once = False
                raise RuntimeError("qdrant acknowledged write then failed")

        vector.before_upsert = publish_newer
        vector.upsert = write_then_fail  # type: ignore[method-assign]

        self.assertTrue(self._worker(vector=vector).run_once())

        self.assertIsNotNone(newer_intent)
        self.assertEqual(self._job_row(intent.job.job_id)["status"], "SUCCEEDED")
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.index_version, 2)
        self.assertEqual(record.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(record.index_status, ShareIndexStatus.READY)
        self.assertEqual(vector.points[intent.record.share_id]["index_version"], 2)
        self.assertEqual(vector.points[intent.record.share_id]["content_hash"], record.content_hash)

    def test_restore_lost_to_unpublish_cleans_restored_version(self) -> None:
        intent, _newer, vector = self._ready_successor_with_stale_old_point()
        triggered = False

        def unpublish_before_restore(
            _share_id: str,
            payload: dict[str, object],
        ) -> None:
            nonlocal triggered
            if triggered or payload["index_version"] != 2:
                return
            triggered = True
            self.store.stage_unpublish(
                intent.record.share_id,
                "user-1",
                now=NOW + timedelta(seconds=3),
            )
            self.assertTrue(
                self._worker(
                    vector=vector,
                    worker_id="worker-delete",
                    clock=MutableClock(NOW + timedelta(seconds=3)),
                ).run_once()
            )

        vector.before_upsert = unpublish_before_restore
        self.assertTrue(
            self._worker(
                vector=vector,
                clock=MutableClock(NOW + timedelta(seconds=4)),
            ).run_once()
        )

        self.assertTrue(triggered)
        self.assertEqual(self._job_row(intent.job.job_id)["status"], "SUCCEEDED")
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(record.index_status, ShareIndexStatus.DELETED)
        self.assertEqual(
            vector.deletes,
            [
                (intent.record.share_id, 2),
                (intent.record.share_id, 1),
                (intent.record.share_id, 2),
            ],
        )
        self.assertNotIn(intent.record.share_id, vector.points)

    def test_restore_cleanup_failure_keeps_unpublished_stale_job_retryable(self) -> None:
        intent, _newer, vector = self._ready_successor_with_stale_old_point()
        triggered = False

        def unpublish_before_restore(
            _share_id: str,
            payload: dict[str, object],
        ) -> None:
            nonlocal triggered
            if triggered or payload["index_version"] != 2:
                return
            triggered = True
            self.store.stage_unpublish(
                intent.record.share_id,
                "user-1",
                now=NOW + timedelta(seconds=3),
            )
            self.assertTrue(
                self._worker(
                    vector=vector,
                    worker_id="worker-delete",
                    clock=MutableClock(NOW + timedelta(seconds=3)),
                ).run_once()
            )
            vector.delete_failure_versions = {2}

        vector.before_upsert = unpublish_before_restore
        self.assertTrue(
            self._worker(
                vector=vector,
                clock=MutableClock(NOW + timedelta(seconds=4)),
            ).run_once()
        )

        self.assertTrue(triggered)
        self.assertEqual(self._job_row(intent.job.job_id)["status"], "PENDING")
        self.assertEqual(self._job_row(intent.job.job_id)["last_error"], "RuntimeError")
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(record.index_status, ShareIndexStatus.DELETED)
        self.assertEqual(
            vector.deletes,
            [(intent.record.share_id, 2), (intent.record.share_id, 1)],
        )
        self.assertEqual(vector.points[intent.record.share_id]["index_version"], 2)

    def test_unpublished_deleted_share_cleans_leftover_stale_upsert(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        self.assertTrue(self._worker(vector=vector).run_once())
        self.store.stage_unpublish(
            intent.record.share_id,
            "user-1",
            now=NOW + timedelta(seconds=1),
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == intent.record.share_id)
                .values(index_status=ShareIndexStatus.DELETED.value)
            )
            connection.execute(
                update(ShareIndexJobRow.__table__)
                .where(ShareIndexJobRow.job_id == intent.job.job_id)
                .values(
                    status=IndexJobStatus.PENDING.value,
                    attempt_count=1,
                    next_retry_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )

        clock = MutableClock(NOW + timedelta(seconds=1))
        self.assertTrue(self._worker(vector=vector, clock=clock).run_once())

        self.assertEqual(self._job_row(intent.job.job_id)["status"], "SUCCEEDED")
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(record.index_status, ShareIndexStatus.DELETED)
        self.assertNotIn(intent.record.share_id, vector.points)

    def test_lost_lease_cleanup_cannot_delete_successor_same_version_point(self) -> None:
        intent = self._intent("a", now=NOW)
        clock = MutableClock()
        vector = FakeVectorIndex()
        triggered = False
        worker_b = self._worker(
            vector=vector,
            worker_id="worker-B",
            clock=clock,
        )

        def expire_and_reclaim(_share_id: str, _payload: dict[str, object]) -> None:
            nonlocal triggered
            if triggered:
                return
            triggered = True
            clock.now = NOW + timedelta(seconds=31)
            self.assertTrue(worker_b.run_once())

        vector.before_upsert = expire_and_reclaim
        worker_a = self._worker(vector=vector, worker_id="worker-A", clock=clock)

        self.assertTrue(worker_a.run_once())
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(record.index_status, ShareIndexStatus.READY)
        point = vector.points.get(intent.record.share_id)
        self.assertIsNotNone(point)
        self.assertEqual(point["index_version"], record.index_version)
        self.assertEqual(point["content_hash"], record.content_hash)

    def test_upsert_rechecks_content_hash_after_embedding(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()

        class MutatingEmbedding(FakeEmbeddingClient):
            def embed(inner_self, text: str) -> list[float]:
                result = super().embed(text)
                with self.engine.begin() as connection:
                    connection.execute(
                        update(SharedGuideRow.__table__)
                        .where(SharedGuideRow.share_id == intent.record.share_id)
                        .values(content_hash="f" * 64)
                    )
                return result

        worker = self._worker(embedding=MutatingEmbedding(), vector=vector)

        self.assertTrue(worker.run_once())
        self.assertEqual(vector.upserts, [])

    def test_old_upsert_cannot_resurrect_an_unpublished_share(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        triggered = False

        def unpublish(_share_id: str, payload: dict[str, object]) -> None:
            nonlocal triggered
            if triggered:
                return
            triggered = True
            self.store.stage_unpublish(
                intent.record.share_id,
                "user-1",
                now=NOW + timedelta(seconds=1),
            )

        vector.before_upsert = unpublish
        self.assertTrue(self._worker(vector=vector).run_once())

        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertNotIn(intent.record.share_id, vector.points)

    def test_old_delete_cannot_remove_newer_republication_out_of_order(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        self.assertTrue(self._worker(vector=vector).run_once())
        self.store.stage_unpublish(
            intent.record.share_id,
            "user-1",
            now=NOW + timedelta(seconds=1),
        )
        triggered = False

        def republish(_share_id: str, index_version: int) -> None:
            nonlocal triggered
            if triggered:
                return
            triggered = True
            self.store.create_publish_intent(
                make_draft(
                    source_session_id="session-a",
                    source_version_number=2,
                    content_marker="b",
                ),
                now=NOW + timedelta(seconds=2),
            )
            self.assertTrue(
                self._worker(
                    vector=vector,
                    worker_id="worker-new",
                    clock=MutableClock(NOW + timedelta(seconds=2)),
                ).run_once()
            )

        vector.before_delete = republish
        self.assertTrue(
            self._worker(
                vector=vector,
                clock=MutableClock(NOW + timedelta(seconds=1)),
            ).run_once()
        )

        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.index_version, 2)
        self.assertEqual(record.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(vector.points[intent.record.share_id]["index_version"], 2)

    def test_delete_is_idempotent_and_never_republishes(self) -> None:
        intent = self._intent("a", now=NOW)
        vector = FakeVectorIndex()
        worker = self._worker(vector=vector)
        self.assertTrue(worker.run_once())
        self.store.stage_unpublish(
            intent.record.share_id,
            "user-1",
            now=NOW + timedelta(seconds=1),
        )

        worker = self._worker(
            vector=vector,
            clock=MutableClock(NOW + timedelta(seconds=1)),
        )
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())

        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(record.index_status, ShareIndexStatus.DELETED)
        self.assertEqual(vector.deletes, [(intent.record.share_id, 1)])

    def test_retry_is_capped_and_final_failure_remains_observable(self) -> None:
        intent = self._intent("a", now=NOW)
        clock = MutableClock()
        embedding = FakeEmbeddingClient(failures=3)
        worker = self._worker(
            embedding=embedding,
            clock=clock,
            max_attempts=3,
            retry_base_seconds=2,
            retry_max_seconds=3,
        )

        self.assertTrue(worker.run_once())
        first = self._job_row(intent.job.job_id)
        self.assertEqual(first["status"], "PENDING")
        self.assertEqual(first["attempt_count"], 1)
        self.assertEqual(first["next_retry_at"], (NOW + timedelta(seconds=2)).replace(tzinfo=None))

        clock.now = NOW + timedelta(seconds=2)
        self.assertTrue(worker.run_once())
        second = self._job_row(intent.job.job_id)
        self.assertEqual(second["attempt_count"], 2)
        self.assertEqual(second["next_retry_at"], (NOW + timedelta(seconds=5)).replace(tzinfo=None))

        clock.now = NOW + timedelta(seconds=5)
        self.assertTrue(worker.run_once())
        final = self._job_row(intent.job.job_id)
        self.assertEqual(final["status"], "FAILED")
        self.assertEqual(final["attempt_count"], 3)
        self.assertIsNone(final["next_retry_at"])
        self.assertEqual(final["last_error"], "RuntimeError")
        record = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(record.index_status, ShareIndexStatus.FAILED)
        self.assertEqual(record.last_index_error, "RuntimeError")

    def test_job_exception_isolation_allows_the_next_job_to_run(self) -> None:
        first = self._intent("a", now=NOW)
        second = self._intent("b", now=NOW + timedelta(seconds=1))
        embedding = FakeEmbeddingClient(failures=1)
        vector = FakeVectorIndex()
        worker = self._worker(
            embedding=embedding,
            vector=vector,
            clock=MutableClock(NOW + timedelta(seconds=1)),
        )

        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        self.assertEqual(self._job_row(first.job.job_id)["status"], "PENDING")
        self.assertEqual(self._job_row(second.job.job_id)["status"], "SUCCEEDED")

    def test_start_stop_are_idempotent_and_shutdown_is_bounded(self) -> None:
        sleep_entered = threading.Event()
        release_sleep = threading.Event()

        def blocking_sleep(_seconds: float) -> None:
            sleep_entered.set()
            release_sleep.wait(timeout=2)

        worker = self._worker(sleep=blocking_sleep, shutdown_timeout_seconds=0.02)
        worker.start()
        self.assertTrue(sleep_entered.wait(timeout=1))
        thread = worker._thread
        worker.start()
        self.assertIs(worker._thread, thread)

        started = time.monotonic()
        worker.stop()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(worker.running)

        release_sleep.set()
        worker.stop()
        worker.stop()
        self.assertFalse(worker.running)


if __name__ == "__main__":
    unittest.main()
