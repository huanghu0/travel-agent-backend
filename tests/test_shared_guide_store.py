"""Transactional shared-guide store tests using file-backed SQLite."""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, event, func, select, update

from app.persistence.sqlalchemy_models import (
    Base,
    ShareIndexJobRow,
    SharedGuideLikeRow,
    SharedGuideRow,
    UserRow,
)
from app.sharing.exceptions import (
    InvalidShareCursorError,
    SharedGuideConflictError,
    SharedGuideForbiddenError,
    SharedGuideNotFoundError,
)
from app.sharing.models import (
    IndexOperation,
    PublicationStatus,
    ShareIndexStatus,
    SharePublishDraft,
    SharedGuideListQuery,
    SharedGuideSnapshot,
    SharedTripRequestSnapshot,
)
from app.sharing.mysql_store import MySQLSharedGuideStore


UTC = timezone.utc
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def make_draft(
    *,
    author_user_id: str = "user-1",
    source_session_id: str = "session-1",
    source_version_number: int = 1,
    title: str = "杭州周末攻略",
    city: str = "杭州",
    city_normalized: str = "hangzhou",
    transportation: str = "transit",
    content_marker: str = "a",
) -> SharePublishDraft:
    content_hash = content_marker * 64
    request = SharedTripRequestSnapshot(
        city=city,
        travel_days=2,
        transportation=transportation,
        accommodation="hotel",
        preferences=["food", "nature"],
    )
    snapshot = SharedGuideSnapshot.model_validate(
        {
            "request": request.model_dump(),
            "trip_plan": {
                "city": city,
                "start_date": "2026-08-01",
                "end_date": "2026-08-02",
                "days": [
                    {
                        "date": "2026-08-01",
                        "day_index": 0,
                        "description": f"day-{content_marker}",
                        "transportation": transportation,
                        "accommodation": "hotel",
                        "attractions": [
                            {
                                "name": "西湖",
                                "address": "西湖风景区",
                                "location": {"longitude": 120.15, "latitude": 30.25},
                                "visit_duration": 90,
                                "description": "湖景",
                                "image_url": "https://images.example/west-lake.jpg",
                            }
                        ],
                        "meals": [],
                    }
                ],
                "overall_suggestions": "错峰出行",
            },
        }
    )
    return SharePublishDraft(
        author_user_id=author_user_id,
        source_session_id=source_session_id,
        source_version_id=f"version-{source_session_id}-{source_version_number}",
        source_version_number=source_version_number,
        title=title,
        city=city,
        city_normalized=city_normalized,
        travel_days=2,
        transportation=transportation,
        accommodation="hotel",
        preferences=["food", "nature"],
        snapshot=snapshot,
        retrieval_text=f"{city} guide {content_marker}",
        content_hash=content_hash,
        quality_level="excellent",
        quality_score=92.5,
        embedding_model="qwen3.7-text-embedding",
        embedding_dimension=768,
        retrieval_template_version="retrieval_template_v1",
    )


class SharedGuideStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        database = Path(self.directory.name) / "sharing.db"
        self.engine = create_engine(f"sqlite:///{database}")
        Base.metadata.create_all(self.engine)
        self.store = MySQLSharedGuideStore(self.engine)
        self._add_user("user-1", "alice")

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    def _add_user(self, user_id: str, username: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                UserRow.__table__.insert().values(
                    user_id=user_id,
                    username=username,
                    password_hash="not-used",
                    created_at=NOW.replace(tzinfo=None),
                )
            )

    def _publish_ready(self, draft: SharePublishDraft, *, now: datetime = NOW):
        intent = self.store.create_publish_intent(draft, now=now)
        self.assertIsNotNone(intent.job)
        claimed = self.store.claim_index_job(
            intent.job.job_id,
            "sync:test",
            now=now,
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed)
        completed = self.store.complete_index_operation(
            intent.job.job_id,
            intent.record.share_id,
            intent.record.index_version,
            IndexOperation.UPSERT,
            worker_id="sync:test",
            now=now + timedelta(seconds=1),
        )
        self.assertTrue(completed)
        return self.store.get_owned(intent.record.share_id, draft.author_user_id)

    def _job_count(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                select(func.count()).select_from(ShareIndexJobRow.__table__)
            ).scalar_one()

    def _like_row_count(self, share_id: str) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                select(func.count())
                .select_from(SharedGuideLikeRow.__table__)
                .where(SharedGuideLikeRow.share_id == share_id)
            ).scalar_one()

    def _assert_like_invariant(self, share_id: str) -> None:
        guide = self.store.get_owned(share_id, "user-1")
        self.assertEqual(guide.like_count, self._like_row_count(share_id))

    def test_create_intent_is_atomic_idempotent_and_conflicts_on_changed_active_content(self) -> None:
        draft = make_draft()
        intent = self.store.create_publish_intent(draft, now=NOW)

        self.assertTrue(intent.created)
        self.assertTrue(intent.operation_required)
        self.assertEqual(intent.record.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(intent.record.index_status, ShareIndexStatus.PENDING)
        self.assertEqual(intent.record.index_version, 1)
        self.assertEqual(intent.record.published_at, NOW)
        self.assertIsNotNone(intent.job)
        self.assertEqual(intent.job.operation, IndexOperation.UPSERT)
        self.assertEqual(self._job_count(), 1)

        same = self.store.create_publish_intent(draft, now=NOW + timedelta(seconds=1))
        self.assertFalse(same.created)
        self.assertTrue(same.operation_required)
        self.assertEqual(same.record.share_id, intent.record.share_id)
        self.assertEqual(same.job.job_id, intent.job.job_id)
        self.assertEqual(self._job_count(), 1)

        with self.assertRaises(SharedGuideConflictError):
            self.store.create_publish_intent(
                make_draft(content_marker="b", source_version_number=2),
                now=NOW + timedelta(seconds=2),
            )
        self.assertEqual(self._job_count(), 1)

    def test_claim_and_completion_require_the_exact_current_owned_lease(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)

        claimed = self.store.claim_index_job(
            intent.job.job_id,
            "sync:owner-a",
            now=NOW,
            lease_seconds=30,
        )
        self.assertEqual(claimed.status.value, "RUNNING")
        self.assertEqual(claimed.lease_owner, "sync:owner-a")
        self.assertEqual(claimed.lease_expires_at, NOW + timedelta(seconds=30))
        self.assertEqual(claimed.attempt_count, 1)
        self.assertIsNone(
            self.store.claim_index_job(
                intent.job.job_id,
                "sync:owner-b",
                now=NOW + timedelta(seconds=1),
                lease_seconds=30,
            )
        )

        self.assertFalse(
            self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                99,
                IndexOperation.UPSERT,
                worker_id="sync:owner-a",
                now=NOW + timedelta(seconds=2),
            )
        )
        still_pending = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(still_pending.index_status, ShareIndexStatus.PENDING)

        self.assertTrue(
            self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                worker_id="sync:owner-a",
                now=NOW + timedelta(seconds=2),
            )
        )
        ready = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(ready.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(ready.index_status, ShareIndexStatus.READY)
        self.assertEqual(ready.indexed_at, NOW + timedelta(seconds=2))
        self.assertFalse(
            self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                worker_id="sync:owner-a",
                now=NOW + timedelta(seconds=3),
            )
        )

    def test_update_preserves_likes_and_supersedes_only_an_expired_upsert_lease(self) -> None:
        ready = self._publish_ready(make_draft())
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == ready.share_id)
                .values(like_count=7)
            )

        second = self.store.stage_update(
            ready.share_id,
            "user-1",
            make_draft(content_marker="b", source_version_number=2),
            now=NOW + timedelta(minutes=1),
        )
        self.assertEqual(second.record.index_version, 2)
        self.assertEqual(second.record.like_count, 7)
        self.assertEqual(second.record.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(second.record.index_status, ShareIndexStatus.PENDING)
        self.assertEqual(second.record.published_at, NOW + timedelta(minutes=1))
        self.assertEqual(second.record.created_at, ready.created_at)
        claimed = self.store.claim_index_job(
            second.job.job_id,
            "worker-current",
            now=NOW + timedelta(minutes=1),
            lease_seconds=60,
        )
        self.assertIsNotNone(claimed)

        with self.assertRaises(SharedGuideConflictError):
            self.store.stage_update(
                ready.share_id,
                "user-1",
                make_draft(content_marker="c", source_version_number=3),
                now=NOW + timedelta(minutes=1, seconds=30),
            )

        third = self.store.stage_update(
            ready.share_id,
            "user-1",
            make_draft(content_marker="c", source_version_number=3),
            now=NOW + timedelta(minutes=2, seconds=1),
        )
        self.assertEqual(third.record.index_version, 3)
        self.assertEqual(third.record.like_count, 7)
        self.assertEqual(third.record.content_hash, "c" * 64)
        with self.engine.connect() as connection:
            stale_status = connection.execute(
                select(ShareIndexJobRow.status).where(
                    ShareIndexJobRow.job_id == second.job.job_id
                )
            ).scalar_one()
        self.assertEqual(stale_status, "SUCCEEDED")
        self.assertFalse(
            self.store.complete_index_operation(
                second.job.job_id,
                ready.share_id,
                2,
                IndexOperation.UPSERT,
                worker_id="worker-current",
                now=NOW + timedelta(minutes=2, seconds=2),
            )
        )
        self.assertEqual(self.store.get_owned(ready.share_id, "user-1").index_version, 3)

    def test_stage_update_rejects_active_lease_by_default_but_allows_explicit_supersede(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        old_claim = self.store.claim_index_job(
            intent.job.job_id,
            "sync:old-upsert",
            now=NOW,
            lease_seconds=60,
        )
        self.assertIsNotNone(old_claim)

        with self.assertRaises(SharedGuideConflictError):
            self.store.stage_update(
                intent.record.share_id,
                "user-1",
                make_draft(content_marker="b", source_version_number=2),
                now=NOW + timedelta(seconds=1),
            )

        updated = self.store.stage_update(
            intent.record.share_id,
            "user-1",
            make_draft(content_marker="b", source_version_number=2),
            now=NOW + timedelta(seconds=1),
            allow_active_upsert_supersede=True,
        )
        self.assertEqual(updated.record.index_version, 2)
        self.assertEqual(updated.record.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(updated.record.index_status, ShareIndexStatus.PENDING)
        with self.engine.connect() as connection:
            old_status, old_owner = connection.execute(
                select(
                    ShareIndexJobRow.status,
                    ShareIndexJobRow.lease_owner,
                ).where(ShareIndexJobRow.job_id == intent.job.job_id)
            ).one()
        self.assertEqual(old_status, "RUNNING")
        self.assertEqual(old_owner, "sync:old-upsert")
        self.assertFalse(
            self.store.complete_index_operation(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                worker_id="sync:old-upsert",
                now=NOW + timedelta(seconds=2),
            )
        )

    def test_stage_new_upsert_does_not_overwrite_state_changed_after_share_lock(self) -> None:
        ready = self._publish_ready(make_draft())
        with self.engine.connect() as connection:
            stale_row = connection.execute(
                select(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == ready.share_id)
            ).mappings().one()
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == ready.share_id)
                .values(
                    publication_status=PublicationStatus.PUBLISHING.value,
                    index_status=ShareIndexStatus.FAILED.value,
                )
            )
        with self.engine.begin() as connection:
            with self.assertRaises(SharedGuideConflictError):
                self.store._stage_new_upsert(
                    connection,
                    stale_row,
                    make_draft(content_marker="b", source_version_number=2),
                    now=NOW + timedelta(minutes=1),
                )
        with self.engine.connect() as connection:
            state = connection.execute(
                select(
                    SharedGuideRow.publication_status,
                    SharedGuideRow.index_status,
                    SharedGuideRow.index_version,
                ).where(SharedGuideRow.share_id == ready.share_id)
            ).one()
        self.assertEqual(state.publication_status, PublicationStatus.PUBLISHING.value)
        self.assertEqual(state.index_status, ShareIndexStatus.FAILED.value)
        self.assertEqual(state.index_version, ready.index_version)

    def test_stage_unpublish_does_not_overwrite_state_changed_after_share_lock(self) -> None:
        ready = self._publish_ready(make_draft())
        with self.engine.connect() as connection:
            stale_row = connection.execute(
                select(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == ready.share_id)
            ).mappings().one()
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == ready.share_id)
                .values(
                    publication_status=PublicationStatus.PUBLISHING.value,
                    index_status=ShareIndexStatus.FAILED.value,
                )
            )
        with patch.object(
            MySQLSharedGuideStore,
            "_select_share_for_update",
            return_value=stale_row,
        ):
            with self.assertRaises(SharedGuideConflictError):
                self.store.stage_unpublish(
                    ready.share_id,
                    "user-1",
                    now=NOW + timedelta(minutes=1),
                )
        with self.engine.connect() as connection:
            state = connection.execute(
                select(
                    SharedGuideRow.publication_status,
                    SharedGuideRow.index_status,
                    SharedGuideRow.index_version,
                ).where(SharedGuideRow.share_id == ready.share_id)
            ).one()
        self.assertEqual(state.publication_status, PublicationStatus.PUBLISHING.value)
        self.assertEqual(state.index_status, ShareIndexStatus.FAILED.value)
        self.assertEqual(state.index_version, ready.index_version)

    def test_stale_selected_upsert_job_is_not_superseded_by_job_id_alone(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)

        class InterleavingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.interleaved = False

            def execute(self, statement, *args, **kwargs):
                result = self.connection.execute(statement, *args, **kwargs)
                if (
                    not self.interleaved
                    and "share_index_jobs" in str(statement)
                    and "SELECT" in str(statement).upper()
                ):
                    self.interleaved = True
                    self.connection.execute(
                        update(ShareIndexJobRow.__table__)
                        .where(ShareIndexJobRow.job_id == intent.job.job_id)
                        .values(
                            status="RUNNING",
                            lease_owner="new-owner",
                            lease_expires_at=(NOW + timedelta(minutes=5)).replace(
                                tzinfo=None
                            ),
                        )
                    )
                return result

        with self.engine.begin() as connection:
            self.store._supersede_old_upserts(
                InterleavingConnection(connection),
                intent.record.share_id,
                now=NOW + timedelta(minutes=1),
                reject_active=False,
            )
        with self.engine.connect() as connection:
            job = connection.execute(
                select(
                    ShareIndexJobRow.status,
                    ShareIndexJobRow.lease_owner,
                ).where(ShareIndexJobRow.job_id == intent.job.job_id)
            ).one()
        self.assertEqual(job.status, "RUNNING")
        self.assertEqual(job.lease_owner, "new-owner")

    def test_stage_update_rechecks_claimed_old_job_before_advancing_version(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        original_supersede = MySQLSharedGuideStore._supersede_old_upserts

        class InterleavingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.interleaved = False

            def execute(self, statement, *args, **kwargs):
                result = self.connection.execute(statement, *args, **kwargs)
                if (
                    not self.interleaved
                    and "share_index_jobs" in str(statement)
                    and "SELECT" in str(statement).upper()
                ):
                    self.interleaved = True
                    self.connection.execute(
                        update(ShareIndexJobRow.__table__)
                        .where(ShareIndexJobRow.job_id == intent.job.job_id)
                        .values(
                            status="RUNNING",
                            lease_owner="late-worker",
                            lease_expires_at=(NOW + timedelta(minutes=5)).replace(
                                tzinfo=None
                            ),
                        )
                    )
                return result

        def supersede(connection, share_id, *, now, reject_active):
            return original_supersede(
                InterleavingConnection(connection),
                share_id,
                now=now,
                reject_active=reject_active,
            )

        with patch.object(
            MySQLSharedGuideStore,
            "_supersede_old_upserts",
            side_effect=supersede,
        ):
            with self.assertRaises(SharedGuideConflictError):
                self.store.stage_update(
                    intent.record.share_id,
                    "user-1",
                    make_draft(content_marker="b", source_version_number=2),
                    now=NOW + timedelta(minutes=1),
                )

        current = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(current.index_version, 1)
        self.assertEqual(current.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(current.index_status, ShareIndexStatus.PENDING)
        with self.engine.connect() as connection:
            job = connection.execute(
                select(
                    ShareIndexJobRow.status,
                    ShareIndexJobRow.lease_owner,
                ).where(ShareIndexJobRow.job_id == intent.job.job_id)
            ).one()
        self.assertEqual(job.status, "PENDING")
        self.assertIsNone(job.lease_owner)
        self.assertEqual(self._job_count(), 1)

    def test_stage_update_expected_identity_guard_rejects_newer_authoritative_row(self) -> None:
        ready = self._publish_ready(make_draft(content_marker="a"))
        newer_published_at = NOW + timedelta(seconds=5)
        newer_indexed_at = NOW + timedelta(seconds=6)
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == ready.share_id)
                .values(
                    content_hash="b" * 64,
                    index_version=ready.index_version + 1,
                    published_at=newer_published_at.replace(tzinfo=None),
                    indexed_at=newer_indexed_at.replace(tzinfo=None),
                    updated_at=newer_indexed_at.replace(tzinfo=None),
                )
            )

        with self.assertRaises(SharedGuideConflictError):
            self.store.stage_update(
                ready.share_id,
                "user-1",
                make_draft(content_marker="m", source_version_number=2),
                now=NOW + timedelta(minutes=1),
                expected_index_version=ready.index_version,
                expected_content_hash=ready.content_hash,
                expected_published_at=ready.published_at,
                expected_indexed_at=ready.indexed_at,
            )

        current = self.store.get_owned(ready.share_id, "user-1")
        self.assertEqual(ready.index_version + 1, current.index_version)
        self.assertEqual("b" * 64, current.content_hash)
        self.assertEqual(newer_published_at, current.published_at)
        self.assertEqual(newer_indexed_at, current.indexed_at)

    def test_unpublish_hides_immediately_keeps_version_and_completes_exact_delete(self) -> None:
        ready = self._publish_ready(make_draft())
        intent = self.store.stage_unpublish(
            ready.share_id,
            "user-1",
            now=NOW + timedelta(minutes=1),
        )

        self.assertEqual(intent.record.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(intent.record.index_status, ShareIndexStatus.DELETE_PENDING)
        self.assertEqual(intent.record.index_version, ready.index_version)
        self.assertEqual(intent.job.operation, IndexOperation.DELETE)
        self.assertEqual(intent.job.index_version, ready.index_version)
        with self.assertRaises(SharedGuideNotFoundError):
            self.store.get_public(ready.share_id)
        self.assertEqual(self.store.list_public(SharedGuideListQuery()).items, [])

        claimed = self.store.claim_index_job(
            intent.job.job_id,
            "sync:delete",
            now=NOW + timedelta(minutes=1),
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed)
        self.assertTrue(
            self.store.complete_index_operation(
                intent.job.job_id,
                ready.share_id,
                ready.index_version,
                IndexOperation.DELETE,
                worker_id="sync:delete",
                now=NOW + timedelta(minutes=1, seconds=1),
            )
        )
        deleted = self.store.get_owned(ready.share_id, "user-1")
        self.assertEqual(deleted.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(deleted.index_status, ShareIndexStatus.DELETED)

        repeated = self.store.stage_unpublish(
            ready.share_id,
            "user-1",
            now=NOW + timedelta(minutes=2),
        )
        self.assertFalse(repeated.operation_required)
        self.assertIsNone(repeated.job)

    def test_failure_transitions_are_owned_sanitized_and_preserve_snapshot_fields(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-a",
            now=NOW,
            lease_seconds=30,
        )
        before = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertFalse(
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                RuntimeError("wrong owner"),
                worker_id="worker-b",
                next_retry_at=NOW + timedelta(minutes=1),
                terminal=False,
                now=NOW + timedelta(seconds=1),
            )
        )
        secret = "Authorization: Bearer top-secret\n" + ("provider body " * 200)
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                RuntimeError(secret),
                worker_id="worker-a",
                next_retry_at=NOW + timedelta(minutes=1),
                terminal=False,
                now=NOW + timedelta(seconds=2),
            )
        )
        failed = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertEqual(failed.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(failed.index_status, ShareIndexStatus.FAILED)
        self.assertLessEqual(len(failed.last_index_error), 1000)
        self.assertNotIn("top-secret", failed.last_index_error)
        self.assertEqual(failed.snapshot, before.snapshot)
        self.assertEqual(failed.like_count, before.like_count)
        self.assertEqual(failed.published_at, before.published_at)
        with self.engine.connect() as connection:
            job = connection.execute(
                select(ShareIndexJobRow.__table__).where(
                    ShareIndexJobRow.job_id == intent.job.job_id
                )
            ).mappings().one()
        self.assertEqual(job["status"], "PENDING")
        self.assertEqual(job["attempt_count"], 1)
        self.assertEqual(job["next_retry_at"], (NOW + timedelta(minutes=1)).replace(tzinfo=None))

        self.store.claim_index_job(
            intent.job.job_id,
            "worker-a",
            now=NOW + timedelta(minutes=1),
            lease_seconds=30,
        )
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                ValueError("terminal"),
                worker_id="worker-a",
                next_retry_at=None,
                terminal=True,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
        )
        with self.engine.connect() as connection:
            job = connection.execute(
                select(ShareIndexJobRow.status, ShareIndexJobRow.attempt_count).where(
                    ShareIndexJobRow.job_id == intent.job.job_id
                )
            ).one()
        self.assertEqual(job.status, "FAILED")
        self.assertEqual(job.attempt_count, 2)

    def test_json_error_sanitizer_drops_secret_request_and_provider_fields(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-json",
            now=NOW,
            lease_seconds=30,
        )
        json_error = json.dumps(
            {
                "message": "provider unavailable",
                "api_key": "api-secret-value",
                "authorization": "Bearer bearer-secret-value",
                "request": {
                    "city": "private-request-city",
                    "free_text_input": "private request body",
                },
                "response": {
                    "body": "full-provider-response-body",
                    "token": "response-token-value",
                },
            },
            separators=(",", ":"),
        )
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                RuntimeError(json_error),
                worker_id="worker-json",
                next_retry_at=NOW + timedelta(minutes=1),
                terminal=False,
                now=NOW + timedelta(seconds=1),
            )
        )
        stored = self.store.get_owned(intent.record.share_id, "user-1").last_index_error
        with self.engine.connect() as connection:
            stored_job = connection.execute(
                select(ShareIndexJobRow.last_error).where(
                    ShareIndexJobRow.job_id == intent.job.job_id
                )
            ).scalar_one()
        self.assertEqual(stored, "RuntimeError")
        self.assertEqual(stored_job, "RuntimeError")
        for forbidden in (
            "provider unavailable",
            "api_key",
            "api-secret-value",
            "authorization",
            "Bearer",
            "bearer-secret-value",
            "request",
            "private-request-city",
            "free_text_input",
            "private request body",
            "response",
            "full-provider-response-body",
            "response-token-value",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, stored)
        self.assertLessEqual(len(stored), 1000)

    def test_unlabeled_alphabetic_credential_falls_back_to_type_for_share_and_job(self) -> None:
        intent = self.store.create_publish_intent(
            make_draft(source_session_id="alphabetic-credential-session"),
            now=NOW,
        )
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-alphabetic-credential",
            now=NOW,
            lease_seconds=30,
        )
        credential = "AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEf"
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                RuntimeError(credential),
                worker_id="worker-alphabetic-credential",
                next_retry_at=None,
                terminal=True,
                now=NOW + timedelta(seconds=1),
            )
        )
        stored_share = self.store.get_owned(
            intent.record.share_id,
            "user-1",
        ).last_index_error
        with self.engine.connect() as connection:
            stored_job = connection.execute(
                select(ShareIndexJobRow.last_error).where(
                    ShareIndexJobRow.job_id == intent.job.job_id
                )
            ).scalar_one()
        self.assertEqual(stored_share, "RuntimeError")
        self.assertEqual(stored_job, "RuntimeError")
        self.assertNotIn(credential, stored_share)
        self.assertNotIn(credential, stored_job)
        self.assertLessEqual(len(stored_share), 1000)
        self.assertLessEqual(len(stored_job), 1000)

    def test_bare_provider_credential_error_falls_back_to_type_only(self) -> None:
        intent = self.store.create_publish_intent(
            make_draft(source_session_id="credential-session"),
            now=NOW,
        )
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-credential",
            now=NOW,
            lease_seconds=30,
        )
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                RuntimeError("upstream rejected sk-proj-ABC123SECRET"),
                worker_id="worker-credential",
                next_retry_at=None,
                terminal=True,
                now=NOW + timedelta(seconds=1),
            )
        )
        stored = self.store.get_owned(intent.record.share_id, "user-1").last_index_error
        self.assertEqual(stored, "RuntimeError")
        self.assertLessEqual(len(stored), 1000)

    def test_html_provider_diagnostic_falls_back_to_type_only(self) -> None:
        intent = self.store.create_publish_intent(
            make_draft(source_session_id="diagnostic-session"),
            now=NOW,
        )
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-diagnostic",
            now=NOW,
            lease_seconds=30,
        )
        diagnostic = (
            "<html><section><pre>provider diagnostic trace=diag-ABC123XYZ "
            "status=502</pre></section></html>"
        )
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                1,
                IndexOperation.UPSERT,
                RuntimeError(diagnostic),
                worker_id="worker-diagnostic",
                next_retry_at=None,
                terminal=True,
                now=NOW + timedelta(seconds=1),
            )
        )
        stored = self.store.get_owned(intent.record.share_id, "user-1").last_index_error
        self.assertEqual(stored, "RuntimeError")
        for forbidden in (
            "<html>",
            "provider diagnostic",
            "diag-ABC123XYZ",
            "status=502",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, stored)
        self.assertLessEqual(len(stored), 1000)

    def test_completion_acquires_share_lock_before_job_lock(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-order",
            now=NOW,
            lease_seconds=30,
        )
        lock_order: list[str] = []

        def capture_select(conn, cursor, statement, parameters, context, executemany):
            normalized = statement.upper()
            if "FROM SHARED_GUIDES" in normalized:
                lock_order.append("shared_guides")
            elif "FROM SHARE_INDEX_JOBS" in normalized:
                lock_order.append("share_index_jobs")

        event.listen(self.engine, "before_cursor_execute", capture_select)
        try:
            self.assertTrue(
                self.store.complete_index_operation(
                    intent.job.job_id,
                    intent.record.share_id,
                    intent.record.index_version,
                    IndexOperation.UPSERT,
                    worker_id="worker-order",
                    now=NOW + timedelta(seconds=1),
                )
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_select)
        self.assertEqual(lock_order[:2], ["shared_guides", "share_index_jobs"])

    def test_failure_acquires_share_lock_before_job_lock(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-order",
            now=NOW,
            lease_seconds=30,
        )
        lock_order: list[str] = []

        def capture_select(conn, cursor, statement, parameters, context, executemany):
            normalized = statement.upper()
            if "FROM SHARED_GUIDES" in normalized:
                lock_order.append("shared_guides")
            elif "FROM SHARE_INDEX_JOBS" in normalized:
                lock_order.append("share_index_jobs")

        event.listen(self.engine, "before_cursor_execute", capture_select)
        try:
            self.assertTrue(
                self.store.record_index_failure(
                    intent.job.job_id,
                    intent.record.share_id,
                    intent.record.index_version,
                    IndexOperation.UPSERT,
                    RuntimeError("failure"),
                    worker_id="worker-order",
                    next_retry_at=NOW + timedelta(minutes=1),
                    terminal=False,
                    now=NOW + timedelta(seconds=1),
                )
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_select)
        self.assertEqual(lock_order[:2], ["shared_guides", "share_index_jobs"])

    def test_delete_failure_retry_and_terminal_states(self) -> None:
        ready = self._publish_ready(make_draft())
        intent = self.store.stage_unpublish(ready.share_id, "user-1", now=NOW + timedelta(minutes=1))
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-delete",
            now=NOW + timedelta(minutes=1),
            lease_seconds=30,
        )
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                ready.share_id,
                ready.index_version,
                IndexOperation.DELETE,
                RuntimeError("temporary"),
                worker_id="worker-delete",
                next_retry_at=NOW + timedelta(minutes=2),
                terminal=False,
                now=NOW + timedelta(minutes=1, seconds=1),
            )
        )
        retrying = self.store.get_owned(ready.share_id, "user-1")
        self.assertEqual(retrying.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(retrying.index_status, ShareIndexStatus.DELETE_PENDING)

        self.store.claim_index_job(
            intent.job.job_id,
            "worker-delete",
            now=NOW + timedelta(minutes=2),
            lease_seconds=30,
        )
        self.assertTrue(
            self.store.record_index_failure(
                intent.job.job_id,
                ready.share_id,
                ready.index_version,
                IndexOperation.DELETE,
                RuntimeError("terminal"),
                worker_id="worker-delete",
                next_retry_at=None,
                terminal=True,
                now=NOW + timedelta(minutes=2, seconds=1),
            )
        )
        terminal = self.store.get_owned(ready.share_id, "user-1")
        self.assertEqual(terminal.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(terminal.index_status, ShareIndexStatus.FAILED)

    def test_public_reads_are_redacted_join_username_and_use_stable_keyset_cursors(self) -> None:
        self._add_user("user-2", "bob")
        self._add_user("user-3", "carol")
        records = [
            self._publish_ready(
                make_draft(
                    author_user_id=f"user-{number}",
                    source_session_id=f"session-{number}",
                    title=f"Guide {number}",
                    content_marker=chr(96 + number),
                ),
                now=NOW + timedelta(minutes=number),
            )
            for number in (1, 2, 3)
        ]
        with self.engine.begin() as connection:
            for record, likes in zip(records, (4, 9, 9), strict=True):
                connection.execute(
                    update(SharedGuideRow.__table__)
                    .where(SharedGuideRow.share_id == record.share_id)
                    .values(like_count=likes)
                )

        first_page = self.store.list_public(
            SharedGuideListQuery(sort="latest", limit=2)
        )
        expected_latest = sorted(
            records,
            key=lambda item: (item.published_at, item.share_id),
            reverse=True,
        )
        self.assertEqual(
            [item.share_id for item in first_page.items],
            [item.share_id for item in expected_latest[:2]],
        )
        self.assertEqual(
            [item.author_username for item in first_page.items],
            ["carol", "bob"],
        )
        self.assertNotIn("author_user_id", first_page.items[0].model_dump())
        self.assertEqual(
            first_page.items[0].cover_image_url,
            "https://images.example/west-lake.jpg",
        )
        decoded = json.loads(
            base64.urlsafe_b64decode(first_page.next_cursor + "==").decode("utf-8")
        )
        self.assertEqual(decoded["v"], 1)
        self.assertEqual(decoded["sort"], "latest")
        self.assertEqual(decoded["share_id"], first_page.items[-1].share_id)

        second_page = self.store.list_public(
            SharedGuideListQuery(sort="latest", limit=2, cursor=first_page.next_cursor)
        )
        self.assertEqual(
            [item.share_id for item in second_page.items],
            [expected_latest[2].share_id],
        )
        self.assertIsNone(second_page.next_cursor)

        popular = self.store.list_public(SharedGuideListQuery(sort="popular", limit=3))
        expected_popular = sorted(
            records,
            key=lambda item: (
                {records[0].share_id: 4, records[1].share_id: 9, records[2].share_id: 9}[item.share_id],
                item.published_at,
                item.share_id,
            ),
            reverse=True,
        )
        self.assertEqual(
            [item.share_id for item in popular.items],
            [item.share_id for item in expected_popular],
        )
        detail = self.store.get_public(records[0].share_id)
        self.assertNotIn("author_user_id", detail.model_dump())
        self.assertNotIn("retrieval_text", detail.model_dump())
        self.assertEqual(detail.author_username, "alice")

        invalid_payloads = [
            "not-base64",
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": True,
                        "sort": "latest",
                        "published_at": NOW.isoformat(),
                        "share_id": records[0].share_id,
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode().rstrip("="),
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": 2,
                        "sort": "latest",
                        "published_at": NOW.isoformat(),
                        "share_id": records[0].share_id,
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode().rstrip("="),
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": 1,
                        "sort": "popular",
                        "like_count": 9,
                        "published_at": NOW.isoformat(),
                        "share_id": records[0].share_id,
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode().rstrip("="),
        ]
        for cursor in invalid_payloads:
            with self.subTest(cursor=cursor), self.assertRaises(InvalidShareCursorError):
                self.store.list_public(
                    SharedGuideListQuery(sort="latest", limit=2, cursor=cursor)
                )

    def test_empty_cursor_is_malformed(self) -> None:
        with self.assertRaises(InvalidShareCursorError):
            self.store.list_public(
                SharedGuideListQuery(sort="latest", limit=2, cursor="")
            )

    def test_bulk_ready_rechecks_identity_and_excludes_current_session(self) -> None:
        first = self._publish_ready(make_draft(source_session_id="session-current"))
        self._add_user("user-2", "bob")
        second = self._publish_ready(
            make_draft(
                author_user_id="user-2",
                source_session_id="session-other",
                content_marker="b",
            ),
            now=NOW + timedelta(minutes=1),
        )

        matches = self.store.bulk_get_ready(
            [first, second],
            exclude_session_id="session-current",
        )
        self.assertEqual([item.share_id for item in matches], [second.share_id])

        stale_identity = second.model_copy(update={"index_version": second.index_version + 1})
        self.assertEqual(self.store.bulk_get_ready([stale_identity]), [])

    def test_cross_owner_reads_and_mutations_are_not_found(self) -> None:
        ready = self._publish_ready(make_draft())
        for operation in (
            lambda: self.store.get_owned(ready.share_id, "other-user"),
            lambda: self.store.stage_update(
                ready.share_id,
                "other-user",
                make_draft(author_user_id="other-user", content_marker="b"),
                now=NOW + timedelta(minutes=1),
            ),
            lambda: self.store.stage_unpublish(
                ready.share_id,
                "other-user",
                now=NOW + timedelta(minutes=1),
            ),
        ):
            with self.subTest(operation=operation), self.assertRaises(SharedGuideNotFoundError):
                operation()

    def test_owned_session_read_and_explicit_supersede_do_not_mutate_share(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        by_session = self.store.get_for_author_session("user-1", "session-1")
        self.assertEqual(by_session.share_id, intent.record.share_id)

        owned_page = self.store.list_owned(
            "user-1",
            SharedGuideListQuery(sort="latest", limit=10),
        )
        self.assertEqual([item.share_id for item in owned_page.items], [intent.record.share_id])
        self.assertEqual(owned_page.items[0].publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(owned_page.items[0].index_status, ShareIndexStatus.PENDING)

        before = self.store.get_owned(intent.record.share_id, "user-1")
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == intent.record.share_id)
                .values(
                    index_version=2,
                    publication_status=PublicationStatus.PUBLISHING.value,
                    index_status=ShareIndexStatus.PENDING.value,
                )
            )
        stale_share = self.store.get_owned(intent.record.share_id, "user-1")
        self.assertTrue(
            self.store.supersede_index_job(intent.job.job_id, now=NOW + timedelta(seconds=1))
        )
        self.assertNotEqual(stale_share, before)
        self.assertEqual(self.store.get_owned(intent.record.share_id, "user-1"), stale_share)
        self.assertFalse(
            self.store.supersede_index_job(intent.job.job_id, now=NOW + timedelta(seconds=2))
        )

    def test_supersede_refuses_current_pending_job_and_locks_share_first(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        lock_order: list[str] = []

        def capture_select(conn, cursor, statement, parameters, context, executemany):
            normalized = statement.upper()
            if "FROM SHARED_GUIDES" in normalized:
                lock_order.append("shared_guides")
            elif "FROM SHARE_INDEX_JOBS" in normalized:
                lock_order.append("share_index_jobs")

        event.listen(self.engine, "before_cursor_execute", capture_select)
        try:
            self.assertFalse(
                self.store.supersede_index_job(
                    intent.job.job_id,
                    now=NOW + timedelta(seconds=1),
                )
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_select)
        self.assertEqual(lock_order[:2], ["shared_guides", "share_index_jobs"])
        with self.engine.connect() as connection:
            job = connection.execute(
                select(ShareIndexJobRow.status).where(
                    ShareIndexJobRow.job_id == intent.job.job_id
                )
            ).scalar_one()
        self.assertEqual(job, "PENDING")

    def test_supersede_running_job_requires_owner_or_expired_lease(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        self.store.claim_index_job(
            intent.job.job_id,
            "worker-owner",
            now=NOW,
            lease_seconds=30,
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == intent.record.share_id)
                .values(index_version=2)
            )
        self.assertFalse(
            self.store.supersede_index_job(intent.job.job_id, now=NOW + timedelta(seconds=1))
        )
        self.assertFalse(
            self.store.supersede_index_job(
                intent.job.job_id,
                worker_id="worker-other",
                now=NOW + timedelta(seconds=1),
            )
        )
        self.assertTrue(
            self.store.supersede_index_job(
                intent.job.job_id,
                worker_id="worker-owner",
                now=NOW + timedelta(seconds=1),
            )
        )

    def test_expired_supplied_owner_cannot_supersede_and_reclaim_remains_possible(self) -> None:
        intent = self.store.create_publish_intent(make_draft(), now=NOW)
        claimed = self.store.claim_index_job(
            intent.job.job_id,
            "worker-owner",
            now=NOW,
            lease_seconds=30,
        )
        self.assertIsNotNone(claimed)

        self.assertFalse(
            self.store.supersede_index_job(
                intent.job.job_id,
                worker_id="worker-owner",
                now=NOW + timedelta(seconds=31),
            )
        )
        reclaimed = self.store.claim_index_job(
            intent.job.job_id,
            "worker-reclaimer",
            now=NOW + timedelta(seconds=31),
            lease_seconds=30,
        )
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed.lease_owner, "worker-reclaimer")
        self.assertEqual(reclaimed.attempt_count, 2)

    def test_republish_reuses_identity_increments_version_and_resets_freshness(self) -> None:
        ready = self._publish_ready(make_draft())
        with self.engine.begin() as connection:
            connection.execute(
                update(SharedGuideRow.__table__)
                .where(SharedGuideRow.share_id == ready.share_id)
                .values(like_count=3)
            )
        self.store.stage_unpublish(
            ready.share_id,
            "user-1",
            now=NOW + timedelta(minutes=1),
        )

        republished = self.store.create_publish_intent(
            make_draft(content_marker="b", source_version_number=2),
            now=NOW + timedelta(minutes=2),
        )
        self.assertFalse(republished.created)
        self.assertTrue(republished.operation_required)
        self.assertEqual(republished.record.share_id, ready.share_id)
        self.assertEqual(republished.record.index_version, ready.index_version + 1)
        self.assertEqual(republished.record.like_count, 3)
        self.assertEqual(republished.record.created_at, ready.created_at)
        self.assertEqual(republished.record.published_at, NOW + timedelta(minutes=2))
        self.assertEqual(republished.record.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(republished.job.operation, IndexOperation.UPSERT)

    def test_put_like_is_idempotent_and_keeps_count_equal_to_relations(self) -> None:
        ready = self._publish_ready(make_draft())
        self._add_user("user-2", "bob")

        first = self.store.put_like(ready.share_id, "user-2", now=NOW)
        second = self.store.put_like(
            ready.share_id,
            "user-2",
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(first.like_count, 1)
        self.assertTrue(first.liked)
        self.assertEqual(second.like_count, 1)
        self.assertTrue(second.liked)
        self._assert_like_invariant(ready.share_id)

    def test_delete_like_is_idempotent_and_never_makes_count_negative(self) -> None:
        ready = self._publish_ready(make_draft())
        self._add_user("user-2", "bob")
        self.store.put_like(ready.share_id, "user-2", now=NOW)

        first = self.store.delete_like(ready.share_id, "user-2")
        second = self.store.delete_like(ready.share_id, "user-2")

        self.assertFalse(first.liked)
        self.assertEqual(first.like_count, 0)
        self.assertFalse(second.liked)
        self.assertEqual(second.like_count, 0)
        self._assert_like_invariant(ready.share_id)

    def test_like_rejects_author_and_hidden_targets_without_mutation(self) -> None:
        ready = self._publish_ready(make_draft())
        self._add_user("user-2", "bob")

        with self.assertRaises(SharedGuideForbiddenError):
            self.store.put_like(ready.share_id, "user-1", now=NOW)
        self._assert_like_invariant(ready.share_id)

        self.store.stage_unpublish(
            ready.share_id,
            "user-1",
            now=NOW + timedelta(seconds=1),
        )
        for operation in (
            lambda: self.store.put_like(ready.share_id, "user-2", now=NOW),
            lambda: self.store.delete_like(ready.share_id, "user-2"),
        ):
            with self.subTest(operation=operation), self.assertRaises(SharedGuideNotFoundError):
                operation()
        self._assert_like_invariant(ready.share_id)

    def test_concurrent_duplicate_put_like_creates_one_relation_and_count(self) -> None:
        ready = self._publish_ready(make_draft())
        self._add_user("user-2", "bob")
        barrier = threading.Barrier(3)
        results: list[object] = []
        errors: list[BaseException] = []

        def put_like() -> None:
            try:
                barrier.wait()
                results.append(self.store.put_like(ready.share_id, "user-2", now=NOW))
            except BaseException as exc:  # pragma: no cover - asserted after threads join
                errors.append(exc)

        threads = [threading.Thread(target=put_like), threading.Thread(target=put_like)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.liked for result in results))
        self._assert_like_invariant(ready.share_id)
        self.assertEqual(self._like_row_count(ready.share_id), 1)


if __name__ == "__main__":
    unittest.main()
