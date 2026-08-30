"""Offline contracts for shared-guide RAG maintenance and evaluation commands."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qdrant_client import models

from app.rag.text_builder import EmbeddingTextBuilder
from app.sharing.models import (
    IndexJobStatus,
    IndexOperation,
    PublicationStatus,
    ShareIndexIntent,
    ShareIndexJob,
    ShareIndexStatus,
    SharedGuideRecord,
    SharedGuideSnapshot,
)
from scripts import reconcile_shared_guide_index as reconcile
from scripts import reindex_shared_guides as reindex
from scripts import run_rag_retrieval_evaluation as evaluation


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "rag" / "v1"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def make_snapshot(*, marker: str = "canonical") -> SharedGuideSnapshot:
    return SharedGuideSnapshot.model_validate(
        {
            "request": {
                "city": "北京市",
                "travel_days": 3,
                "transportation": "地铁",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
            },
            "trip_plan": {
                "city": "北京",
                "start_date": "2026-08-01",
                "end_date": "2026-08-03",
                "days": [
                    {
                        "date": "2026-08-01",
                        "day_index": 0,
                        "description": f"故宫与景山 {marker}",
                        "transportation": "公共交通",
                        "accommodation": "经济型酒店",
                        "attractions": [
                            {
                                "name": "故宫",
                                "address": "公开地址",
                                "location": {"longitude": 116.39, "latitude": 39.91},
                                "visit_duration": 180,
                                "description": "历史建筑",
                            }
                        ],
                        "meals": [],
                    }
                ],
                "overall_suggestions": "提前预约。",
            },
        }
    )


def make_record(
    share_id: str,
    *,
    snapshot: SharedGuideSnapshot | None = None,
    content_hash: str | None = None,
    publication_status: PublicationStatus = PublicationStatus.PUBLIC,
    index_status: ShareIndexStatus = ShareIndexStatus.READY,
    index_version: int = 4,
) -> SharedGuideRecord:
    snapshot = snapshot or make_snapshot()
    built = EmbeddingTextBuilder().build_document(snapshot)
    is_ready = index_status is ShareIndexStatus.READY
    is_public = publication_status is PublicationStatus.PUBLIC
    return SharedGuideRecord(
        share_id=share_id,
        author_user_id="owner-1",
        source_session_id=f"session-{share_id}",
        source_version_id=f"version-{share_id}",
        source_version_number=1,
        title="北京三日攻略",
        city="北京",
        city_normalized="北京",
        travel_days=3,
        transportation="公共交通",
        accommodation="经济型酒店",
        preferences=["历史文化", "美食"],
        snapshot=snapshot,
        retrieval_text=built.text,
        content_hash=content_hash or built.content_hash,
        quality_level="excellent",
        quality_score=95.0,
        embedding_model="qwen3.7-text-embedding",
        embedding_dimension=768,
        retrieval_template_version=built.template_version,
        publication_status=publication_status,
        index_status=index_status,
        index_version=index_version,
        like_count=2,
        indexed_at=NOW if is_ready else None,
        published_at=NOW if is_public else None,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeEmbeddingClient:
    model = "qwen3.7-text-embedding"
    dimension = 768

    def __init__(self, *, fail_calls: set[int] | None = None) -> None:
        self.calls: list[str] = []
        self.fail_calls = fail_calls or set()

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if len(self.calls) in self.fail_calls:
            raise RuntimeError("offline provider failure secret=never-log")
        return [0.01] * self.dimension


class FakeMaintenanceStore:
    def __init__(
        self,
        records: list[SharedGuideRecord],
        *,
        authoritative_reads: dict[str, list[SharedGuideRecord | None]] | None = None,
        authoritative_errors: dict[str, list[BaseException]] | None = None,
        require_stage_expected: bool = False,
    ) -> None:
        self.records = sorted(records, key=lambda item: item.share_id)
        self.read_calls: list[tuple[str | None, int, str | None]] = []
        self.authoritative_reads = {
            share_id: list(sequence)
            for share_id, sequence in (authoritative_reads or {}).items()
        }
        self.authoritative_errors = {
            share_id: list(sequence)
            for share_id, sequence in (authoritative_errors or {}).items()
        }
        self.require_stage_expected = require_stage_expected
        self.stage_expected: dict[str, object] | None = None
        self.authoritative_read_calls: list[str] = []
        self.requeue_calls: list[tuple] = []
        self.write_calls: list[tuple] = []

    def list_active_public(
        self,
        *,
        after_share_id: str | None,
        limit: int,
        share_id: str | None = None,
    ) -> list[SharedGuideRecord]:
        self.read_calls.append((after_share_id, limit, share_id))
        rows = [row for row in self.records if after_share_id is None or row.share_id > after_share_id]
        if share_id is not None:
            rows = [row for row in rows if row.share_id == share_id]
        return rows[:limit]

    def get_index_record(self, share_id: str) -> SharedGuideRecord | None:
        self.authoritative_read_calls.append(share_id)
        errors = self.authoritative_errors.get(share_id)
        if errors:
            raise errors.pop(0)
        sequence = self.authoritative_reads.get(share_id)
        if sequence:
            return sequence.pop(0)
        return next((row for row in self.records if row.share_id == share_id), None)

    def requeue_current_upsert(self, share_id, index_version, content_hash, *, now):
        self.requeue_calls.append((share_id, index_version, content_hash, now))
        self.write_calls.append(("requeue", share_id, index_version, content_hash))
        return True

    def stage_update(self, share_id, author_user_id, draft, *, now, **kwargs):
        self.write_calls.append(("stage_update", share_id, author_user_id, draft, kwargs))
        if self.require_stage_expected:
            expected_names = (
                "expected_index_version",
                "expected_content_hash",
                "expected_published_at",
                "expected_indexed_at",
            )
            if any(name not in kwargs for name in expected_names):
                raise AssertionError("maintenance stage_update requires expected identity")
            self.stage_expected = {
                name: kwargs[name]
                for name in expected_names
            }
        current = next(row for row in self.records if row.share_id == share_id)
        staged = SharedGuideRecord(
            **draft.model_dump(),
            share_id=current.share_id,
            publication_status=PublicationStatus.PUBLISHING,
            index_status=ShareIndexStatus.PENDING,
            index_version=current.index_version + 1,
            like_count=current.like_count,
            published_at=now,
            created_at=current.created_at,
            updated_at=now,
        )
        job = ShareIndexJob(
            job_id=f"job-{share_id}",
            share_id=share_id,
            operation=IndexOperation.UPSERT,
            index_version=staged.index_version,
            created_at=now,
            updated_at=now,
        )
        return ShareIndexIntent(record=staged, job=job, created=False, operation_required=True)

    def claim_index_job(self, job_id, worker_id, *, now, lease_seconds):
        self.write_calls.append(("claim", job_id, worker_id, lease_seconds))
        share_id = job_id.removeprefix("job-")
        current = next(row for row in self.records if row.share_id == share_id)
        return ShareIndexJob(
            job_id=job_id,
            share_id=share_id,
            operation=IndexOperation.UPSERT,
            index_version=current.index_version + 1,
            status=IndexJobStatus.RUNNING,
            attempt_count=1,
            lease_owner=worker_id,
            lease_expires_at=now,
            created_at=now,
            updated_at=now,
        )

    def complete_index_operation(self, *args, **kwargs):
        self.write_calls.append(("complete", args, kwargs))
        return True

    def record_index_failure(self, *args, **kwargs):
        self.write_calls.append(("failure", args, kwargs))
        return True


def fake_physical_id(point: reconcile.IndexedPoint):
    point_id = getattr(point, "point_id", None)
    return point.share_id if point_id is None else point_id


class FakeMaintenanceIndex:
    def __init__(self, points: list[reconcile.IndexedPoint] | None = None) -> None:
        self.points = sorted(
            points or [],
            key=lambda item: (item.share_id or "", str(fake_physical_id(item))),
        )
        self.read_calls: list[tuple[str, str | None, int]] = []
        self.point_reads: list[tuple[str, object]] = []
        self.point_by_id = {
            fake_physical_id(point): point
            for point in self.points
        }
        self.upserts: list[tuple] = []
        self.deletes: list[tuple] = []
        self.conditional_delete_calls: list[tuple] = []
        self.deleted_point_ids: list[object] = []
        self.identity_deletes: list[tuple] = []

    def scroll(self, *, collection: str, offset: str | None, limit: int):
        self.read_calls.append((collection, offset, limit))
        start = 0 if offset is None else next(
            (
                index + 1
                for index, point in enumerate(self.points)
                if fake_physical_id(point) == offset
            ),
            len(self.points),
        )
        page = self.points[start : start + limit]
        next_offset = (
            fake_physical_id(page[-1])
            if start + len(page) < len(self.points)
            else None
        )
        return page, next_offset

    def upsert(self, share_id, vector, *, payload):
        self.upserts.append((share_id, tuple(vector), payload))

    def read_point(self, *, collection: str, point_id: object):
        self.point_reads.append((collection, point_id))
        return self.point_by_id.get(point_id)

    def delete_point_if_matches(self, *, collection: str, point: reconcile.IndexedPoint):
        self.conditional_delete_calls.append((collection, point))
        if (
            point.malformed_reason is not None
            or point.share_id is None
            or point.index_version is None
            or point.content_hash is None
        ):
            return False
        current = self.point_by_id.get(fake_physical_id(point))
        if current is None or any(
            (
                getattr(current, name) != getattr(point, name)
                for name in (
                    "share_id",
                    "index_version",
                    "content_hash",
                    "visibility",
                    "malformed_reason",
                )
            )
        ):
            return False
        self.delete_point(collection=collection, point=point)
        return True

    def delete_point(self, *, collection: str, point: reconcile.IndexedPoint):
        self.deletes.append((collection, point.share_id, point.index_version))
        point_id = getattr(point, "point_id", point.share_id)
        self.deleted_point_ids.append(point_id)
        self.point_by_id.pop(point_id, None)

    def delete(self, share_id: str, *, index_version: int) -> None:
        self.identity_deletes.append((share_id, index_version))


class FakeCollectionClient:
    def __init__(self, *, dimension: int, distance: object, exists: bool = True) -> None:
        self.dimension = dimension
        self.distance = distance
        self.exists = exists
        self.collection_exists_calls: list[str] = []
        self.get_collection_calls: list[str] = []

    def collection_exists(self, collection_name: str) -> bool:
        self.collection_exists_calls.append(collection_name)
        return self.exists

    def get_collection(self, *, collection_name: str):
        self.get_collection_calls.append(collection_name)
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=models.VectorParams(size=self.dimension, distance=self.distance),
                ),
            ),
        )


def make_inconsistent_record(
    share_id: str,
    *,
    missing: str,
) -> SharedGuideRecord:
    record = make_record(share_id)
    values = record.model_dump()
    values[missing] = None
    return SharedGuideRecord.model_construct(**values)


class RagMaintenanceCommandTests(unittest.TestCase):
    def test_parsers_default_to_dry_run_and_lock_destructive_flags(self) -> None:
        reindex_args = reindex.parse_args([])
        reconcile_args = reconcile.parse_args([])

        self.assertFalse(reindex_args.apply)
        self.assertEqual(100, reindex_args.batch_size)
        self.assertIsNone(reindex_args.share_id)
        self.assertFalse(reconcile_args.apply)
        self.assertFalse(reconcile_args.delete_extra)
        self.assertEqual(100, reconcile_args.batch_size)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            reconcile.parse_args(["--delete-extra"])

    def test_collection_validation_rejects_empty_placeholder_or_unknown_names(self) -> None:
        for value in ("", " ", "default", "unknown", "collection"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reindex.validate_collection_name(value)
        self.assertEqual(
            "shared_guide_embeddings_v1",
            reindex.validate_collection_name("shared_guide_embeddings_v1"),
        )
        self.assertEqual(
            "shared_guide_embeddings_v2",
            reconcile.validate_collection_name("shared_guide_embeddings_v2"),
        )

    def test_reindex_dry_run_is_bounded_filters_active_records_and_never_writes(self) -> None:
        active = make_record("00000000-0000-0000-0000-000000000001")
        cancelled = make_record(
            "00000000-0000-0000-0000-000000000002",
            publication_status=PublicationStatus.UNPUBLISHED,
            index_status=ShareIndexStatus.DELETED,
        )
        pending = make_record(
            "00000000-0000-0000-0000-000000000003",
            publication_status=PublicationStatus.PUBLISHING,
            index_status=ShareIndexStatus.PENDING,
        )
        store = FakeMaintenanceStore([active, cancelled, pending])
        embedding = FakeEmbeddingClient()
        index = FakeMaintenanceIndex()

        report = reindex.run_reindex(
            reindex.parse_args(["--batch-size", "2"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=embedding,
            vector_index=index,
            clock=lambda: NOW,
            output=io.StringIO(),
        )

        self.assertTrue(report.dry_run)
        self.assertEqual(1, report.selected)
        self.assertEqual([2, 2], [call[1] for call in store.read_calls])
        self.assertEqual([], embedding.calls)
        self.assertEqual([], store.write_calls)
        self.assertEqual([], index.upserts)
        self.assertEqual([], index.deletes)

    def test_reindex_same_hash_rebuilds_same_version_without_visibility_change(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000011")
        store = FakeMaintenanceStore([record])
        embedding = FakeEmbeddingClient()
        index = FakeMaintenanceIndex()

        report = reindex.run_reindex(
            reindex.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=embedding,
            vector_index=index,
            clock=lambda: NOW,
            output=io.StringIO(),
        )

        self.assertEqual(0, report.exit_code)
        self.assertEqual(1, report.rebuilt_same_version)
        self.assertEqual([], store.write_calls)
        self.assertEqual(record.index_version, index.upserts[0][2]["index_version"])
        self.assertEqual("PUBLIC", index.upserts[0][2]["visibility"])
        self.assertEqual(PublicationStatus.PUBLIC, record.publication_status)
        self.assertEqual(ShareIndexStatus.READY, record.index_status)

    def test_reindex_changed_hash_uses_normal_version_flow_and_continues_after_failure(self) -> None:
        first = make_record(
            "00000000-0000-0000-0000-000000000021",
            content_hash="f" * 64,
        )
        second = make_record(
            "00000000-0000-0000-0000-000000000022",
            content_hash="e" * 64,
        )
        store = FakeMaintenanceStore([first, second])
        embedding = FakeEmbeddingClient(fail_calls={1})
        index = FakeMaintenanceIndex()

        report = reindex.run_reindex(
            reindex.parse_args(["--apply", "--batch-size", "1"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=embedding,
            vector_index=index,
            clock=lambda: NOW,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.exit_code)
        self.assertEqual(2, report.changed_hash)
        self.assertEqual(1, report.failed)
        self.assertEqual(1, report.reindexed)
        staged = [call for call in store.write_calls if call[0] == "stage_update"]
        self.assertEqual([first.share_id, second.share_id], [call[1] for call in staged])
        self.assertEqual(second.index_version + 1, index.upserts[0][2]["index_version"])

    def test_reindex_changed_hash_passes_scanned_identity_to_transactional_stage_guard(self) -> None:
        record = make_record(
            "00000000-0000-0000-0000-000000000023",
            content_hash="f" * 64,
        )
        store = FakeMaintenanceStore([record], require_stage_expected=True)
        report = reindex.run_reindex(
            reindex.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=FakeEmbeddingClient(),
            vector_index=FakeMaintenanceIndex(),
            clock=lambda: NOW,
            output=io.StringIO(),
        )

        self.assertEqual(0, report.exit_code)
        self.assertEqual(1, report.reindexed)
        self.assertEqual(
            {
                "expected_index_version": record.index_version,
                "expected_content_hash": record.content_hash,
                "expected_published_at": record.published_at,
                "expected_indexed_at": record.indexed_at,
            },
            store.stage_expected,
        )

    def test_reconcile_paginates_and_reports_missing_stale_and_extra_separately(self) -> None:
        first = make_record("00000000-0000-0000-0000-000000000031")
        second = make_record("00000000-0000-0000-0000-000000000032")
        extra_id = "00000000-0000-0000-0000-000000000099"
        points = [
            reconcile.IndexedPoint(
                share_id=second.share_id,
                index_version=second.index_version - 1,
                content_hash="b" * 64,
            ),
            reconcile.IndexedPoint(
                share_id=extra_id,
                index_version=1,
                content_hash="c" * 64,
            ),
        ]
        store = FakeMaintenanceStore([first, second])
        index = FakeMaintenanceIndex(points)
        embedding = FakeEmbeddingClient()
        output = io.StringIO()

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--batch-size", "1"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=embedding,
            vector_index=index,
            output=output,
        )

        self.assertEqual([first.share_id], report.missing)
        self.assertEqual([second.share_id], report.stale)
        self.assertEqual([extra_id], report.extra)
        self.assertGreaterEqual(len(store.read_calls), 2)
        self.assertGreaterEqual(len(index.read_calls), 2)
        self.assertEqual([], embedding.calls)
        self.assertEqual([], index.upserts)
        self.assertEqual([], index.deletes)
        self.assertIn(f"missing share_id={first.share_id}", output.getvalue())
        self.assertIn(f"stale share_id={second.share_id}", output.getvalue())
        self.assertIn(f"extra share_id={extra_id}", output.getvalue())

    def test_reconcile_repairs_only_apply_and_deletes_extra_only_with_both_flags(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000041")
        extra = reconcile.IndexedPoint(
            share_id="00000000-0000-0000-0000-000000000049",
            index_version=7,
            content_hash="d" * 64,
        )

        no_delete_index = FakeMaintenanceIndex([extra])
        reconcile.run_reconcile(
            reconcile.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([record]),
            embedding_client=FakeEmbeddingClient(),
            vector_index=no_delete_index,
            output=io.StringIO(),
        )
        self.assertEqual(1, len(no_delete_index.upserts))
        self.assertEqual([], no_delete_index.deletes)

        deleting_index = FakeMaintenanceIndex([extra])
        reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([record]),
            embedding_client=FakeEmbeddingClient(),
            vector_index=deleting_index,
            output=io.StringIO(),
        )
        self.assertEqual([( "shared_guide_embeddings_v1", extra.share_id, 7)], deleting_index.deletes)

    def test_reindex_same_hash_skips_when_authoritative_record_changes_before_write(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000051")
        publishing = make_record(
            record.share_id,
            publication_status=PublicationStatus.PUBLISHING,
            index_status=ShareIndexStatus.PENDING,
            index_version=record.index_version + 1,
        )
        store = FakeMaintenanceStore(
            [record],
            authoritative_reads={record.share_id: [record, publishing]},
        )
        index = FakeMaintenanceIndex()

        report = reindex.run_reindex(
            reindex.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            clock=lambda: NOW,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.skipped)
        self.assertEqual(0, report.rebuilt_same_version)
        self.assertEqual([], index.upserts)
        self.assertEqual(2, len(store.authoritative_read_calls))

    def test_reindex_same_hash_cleans_stale_write_after_authoritative_change(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000052")
        updated = make_record(
            record.share_id,
            content_hash="a" * 64,
            index_version=record.index_version + 1,
        )
        store = FakeMaintenanceStore(
            [record],
            authoritative_reads={record.share_id: [record, record, updated]},
        )
        index = FakeMaintenanceIndex()

        report = reindex.run_reindex(
            reindex.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            clock=lambda: NOW,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.failed)
        self.assertEqual([(record.share_id, record.index_version)], index.identity_deletes)

    def test_reindex_authoritative_read_failure_is_a_failed_apply_not_a_concurrent_skip(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000059")
        store = FakeMaintenanceStore(
            [record],
            authoritative_errors={record.share_id: [RuntimeError("database unavailable")]},
        )
        index = FakeMaintenanceIndex()

        report = reindex.run_reindex(
            reindex.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            clock=lambda: NOW,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.exit_code)
        self.assertEqual(1, report.failed)
        self.assertEqual(0, report.skipped)
        self.assertEqual([], index.upserts)

    def test_reconcile_repairs_skip_when_authoritative_record_changes_before_write(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000053")
        publishing = make_record(
            record.share_id,
            publication_status=PublicationStatus.PUBLISHING,
            index_status=ShareIndexStatus.PENDING,
            index_version=record.index_version + 1,
        )
        store = FakeMaintenanceStore(
            [record],
            authoritative_reads={record.share_id: [record, publishing]},
        )
        index = FakeMaintenanceIndex()

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.skipped)
        self.assertEqual([], index.upserts)

    def test_reconcile_does_not_delete_extra_point_for_publishing_authoritative_row(self) -> None:
        publishing = make_record(
            "00000000-0000-0000-0000-000000000054",
            publication_status=PublicationStatus.PUBLISHING,
            index_status=ShareIndexStatus.PENDING,
        )
        point = reconcile.IndexedPoint(
            point_id=publishing.share_id,
            share_id=publishing.share_id,
            index_version=publishing.index_version,
            content_hash=publishing.content_hash,
        )
        store = FakeMaintenanceStore(
            [publishing],
            authoritative_reads={publishing.share_id: [publishing]},
        )
        index = FakeMaintenanceIndex([point])

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.skipped)
        self.assertEqual([], index.deleted_point_ids)

    def test_reconcile_repair_cleans_stale_write_after_authoritative_change(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000061")
        updated = make_record(
            record.share_id,
            content_hash="c" * 64,
            index_version=record.index_version + 1,
        )
        store = FakeMaintenanceStore(
            [record],
            authoritative_reads={record.share_id: [record, record, updated, updated]},
        )
        index = FakeMaintenanceIndex()

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.failed)
        self.assertEqual([(record.share_id, record.index_version)], index.identity_deletes)
        self.assertEqual(1, len(store.requeue_calls))

    def test_reconcile_authoritative_read_failure_is_a_failed_apply_not_a_concurrent_skip(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000064")
        store = FakeMaintenanceStore(
            [record],
            authoritative_errors={record.share_id: [RuntimeError("database unavailable")]},
        )

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=FakeMaintenanceIndex(),
            output=io.StringIO(),
        )

        self.assertEqual(1, report.exit_code)
        self.assertEqual(1, report.failed)
        self.assertEqual(0, report.skipped)
        self.assertEqual([], store.write_calls)

    def test_reconcile_extra_delete_db_read_failure_is_failed_and_does_not_delete(self) -> None:
        share_id = "00000000-0000-0000-0000-000000000065"
        point = reconcile.IndexedPoint(
            point_id="physical-extra-db-failure",
            share_id=share_id,
            index_version=1,
            content_hash="a" * 64,
        )
        store = FakeMaintenanceStore(
            [],
            authoritative_errors={share_id: [RuntimeError("database unavailable")]},
        )
        index = FakeMaintenanceIndex([point])

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.exit_code)
        self.assertEqual(1, report.failed)
        self.assertEqual(0, report.skipped)
        self.assertEqual([], index.deleted_point_ids)

    def test_reconcile_does_not_delete_repaired_physical_point_after_scroll_snapshot_changes(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000066")
        old_point = reconcile.IndexedPoint(
            point_id="physical-shared-by-repair",
            share_id="00000000-0000-0000-0000-000000000067",
            index_version=1,
            content_hash="a" * 64,
        )

        class RepairMutatesScrolledPointIndex(FakeMaintenanceIndex):
            def upsert(self, share_id, vector, *, payload):
                super().upsert(share_id, vector, payload=payload)
                self.point_by_id[old_point.point_id] = reconcile.IndexedPoint(
                    point_id=old_point.point_id,
                    share_id=share_id,
                    index_version=payload["index_version"],
                    content_hash=payload["content_hash"],
                )

        index = RepairMutatesScrolledPointIndex([old_point])
        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([record]),
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual(1, report.repaired)
        self.assertEqual(0, report.deleted)
        self.assertGreaterEqual(report.skipped, 1)
        self.assertEqual([], index.deleted_point_ids)
        self.assertIn(old_point.point_id, [point_id for _, point_id in index.point_reads])

    def test_reconcile_does_not_delete_point_replaced_after_read_and_db_guard(self) -> None:
        old_point = reconcile.IndexedPoint(
            point_id="physical-replaced-after-guard",
            share_id="00000000-0000-0000-0000-000000000069",
            index_version=1,
            content_hash="a" * 64,
        )
        replacement = reconcile.IndexedPoint(
            point_id=old_point.point_id,
            share_id="00000000-0000-0000-0000-000000000070",
            index_version=9,
            content_hash="b" * 64,
        )
        index = FakeMaintenanceIndex([old_point])

        class ReplaceAfterGuardStore(FakeMaintenanceStore):
            def __init__(self):
                super().__init__([])
                self.replaced = False

            def get_index_record(self, share_id: str):
                current = super().get_index_record(share_id)
                if not self.replaced and share_id == old_point.share_id:
                    self.replaced = True
                    index.point_by_id[old_point.point_id] = replacement
                return current

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=ReplaceAfterGuardStore(),
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual(0, report.failed)
        self.assertEqual(0, report.deleted)
        self.assertEqual(1, report.skipped)
        self.assertEqual([], index.deleted_point_ids)
        self.assertEqual(1, len(index.conditional_delete_calls))

    def test_reconcile_does_not_count_point_replaced_after_conditional_delete(self) -> None:
        old_share_id = "00000000-0000-0000-0000-000000000071"
        replacement_share_id = "00000000-0000-0000-0000-000000000072"
        physical_id = "physical-replaced-after-conditional-delete"
        old_payload = {
            "share_id": old_share_id,
            "index_version": 1,
            "content_hash": "a" * 64,
            "visibility": "PUBLIC",
        }
        replacement_payload = {
            "share_id": replacement_share_id,
            "index_version": 9,
            "content_hash": "b" * 64,
            "visibility": "PRIVATE",
        }

        class FakeQdrantClient:
            def __init__(self) -> None:
                self.scanned = SimpleNamespace(id=physical_id, payload=old_payload)
                self.current = self.scanned
                self.delete_calls: list[dict] = []

            def scroll(self, **kwargs):
                return [self.scanned], None

            def retrieve(self, **kwargs):
                return [] if self.current is None else [self.current]

            def delete(self, **kwargs):
                self.delete_calls.append(kwargs)

        client = FakeQdrantClient()
        replacement = SimpleNamespace(id=physical_id, payload=replacement_payload)

        class ReplaceBeforeConditionalDeleteIndex(reconcile.QdrantMaintenanceIndex):
            def delete_point_if_matches(self, *, collection: str, point):
                # The race occurs after reconcile's physical re-read and DB guard,
                # immediately before the conditional delete request is sent.
                client.current = replacement
                return super().delete_point_if_matches(
                    collection=collection,
                    point=point,
                )

        index = ReplaceBeforeConditionalDeleteIndex(
            client=client,
            collection="shared_guide_embeddings_v1",
            dimension=768,
        )
        output = io.StringIO()
        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([]),
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=output,
        )

        self.assertEqual(0, report.exit_code)
        self.assertEqual(0, report.failed)
        self.assertEqual(0, report.deleted)
        self.assertEqual(1, report.skipped)
        self.assertIn("replaced", output.getvalue())
        self.assertEqual(1, len(client.delete_calls))

    def test_reconcile_reports_present_non_string_visibility_as_malformed(self) -> None:
        for visibility in (None, 7):
            with self.subTest(visibility=visibility):
                share_id = (
                    "00000000-0000-0000-0000-000000000073"
                    if visibility is None
                    else "00000000-0000-0000-0000-000000000074"
                )
                physical_id = f"malformed-visibility-{visibility!r}"

                class FakeQdrantClient:
                    def __init__(self) -> None:
                        self.point = SimpleNamespace(
                            id=physical_id,
                            payload={
                                "share_id": share_id,
                                "index_version": 1,
                                "content_hash": "c" * 64,
                                "visibility": visibility,
                            },
                        )
                        self.delete_calls: list[dict] = []

                    def scroll(self, **kwargs):
                        return [self.point], None

                    def retrieve(self, **kwargs):
                        return [self.point]

                    def delete(self, **kwargs):
                        self.delete_calls.append(kwargs)

                client = FakeQdrantClient()
                index = reconcile.QdrantMaintenanceIndex(
                    client=client,
                    collection="shared_guide_embeddings_v1",
                    dimension=768,
                )
                points, _ = index.scroll(
                    collection="shared_guide_embeddings_v1",
                    offset=None,
                    limit=100,
                )
                self.assertEqual(visibility, points[0].visibility)
                self.assertIsNotNone(points[0].malformed_reason)

                output = io.StringIO()
                report = reconcile.run_reconcile(
                    reconcile.parse_args(["--apply", "--delete-extra"]),
                    collection="shared_guide_embeddings_v1",
                    store=FakeMaintenanceStore([]),
                    embedding_client=FakeEmbeddingClient(),
                    vector_index=index,
                    output=output,
                )

                self.assertEqual(0, report.exit_code)
                self.assertEqual([physical_id], report.malformed)
                self.assertEqual(0, report.deleted)
                self.assertEqual(1, report.skipped)
                self.assertEqual([], client.delete_calls)
                self.assertIn("malformed", output.getvalue())

    def test_reconcile_allows_exact_terminal_unpublished_cleanup_without_republishing(self) -> None:
        terminal = make_record(
            "00000000-0000-0000-0000-000000000068",
            publication_status=PublicationStatus.UNPUBLISHED,
            index_status=ShareIndexStatus.DELETED,
        )
        point = reconcile.IndexedPoint(
            point_id=terminal.share_id,
            share_id=terminal.share_id,
            index_version=terminal.index_version,
            content_hash=terminal.content_hash,
        )
        store = FakeMaintenanceStore([terminal])
        index = FakeMaintenanceIndex([point])

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual(0, report.exit_code)
        self.assertEqual(1, report.deleted)
        self.assertEqual([], store.requeue_calls)
        self.assertEqual([terminal.share_id], index.deleted_point_ids)

    def test_reconcile_reports_and_requeues_authoritative_reappearance_after_delete(self) -> None:
        share_id = "00000000-0000-0000-0000-000000000062"
        reappeared = make_record(share_id)
        point = reconcile.IndexedPoint(
            point_id=share_id,
            share_id=share_id,
            index_version=reappeared.index_version,
            content_hash=reappeared.content_hash,
        )
        store = FakeMaintenanceStore(
            [],
            authoritative_reads={share_id: [None, reappeared]},
        )
        index = FakeMaintenanceIndex([point])

        report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=index,
            output=io.StringIO(),
        )

        self.assertEqual([share_id], report.extra)
        self.assertEqual(1, report.deleted)
        self.assertEqual(1, report.failed)
        self.assertEqual(1, report.requeued)
        self.assertEqual([share_id], index.deleted_point_ids)

    def test_reconcile_preserves_physical_ids_and_reports_malformed_points(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000055")
        rogue = reconcile.IndexedPoint(
            point_id="rogue-physical-id",
            share_id=record.share_id,
            index_version=record.index_version,
            content_hash=record.content_hash,
        )
        malformed = reconcile.IndexedPoint(
            point_id="malformed-physical-id",
            share_id=None,
            index_version=None,
            content_hash=None,
            malformed_reason="missing share_id",
        )
        store = FakeMaintenanceStore([record])
        dry_index = FakeMaintenanceIndex([rogue, malformed])
        dry_report = reconcile.run_reconcile(
            reconcile.parse_args([]),
            collection="shared_guide_embeddings_v1",
            store=store,
            embedding_client=FakeEmbeddingClient(),
            vector_index=dry_index,
            output=io.StringIO(),
        )

        self.assertEqual(["rogue-physical-id"], dry_report.wrong_id)
        self.assertEqual(["malformed-physical-id"], dry_report.malformed)
        self.assertEqual([], dry_report.extra)
        self.assertEqual([], dry_index.deleted_point_ids)

        apply_index = FakeMaintenanceIndex([rogue, malformed])
        apply_report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([record]),
            embedding_client=FakeEmbeddingClient(),
            vector_index=apply_index,
            output=io.StringIO(),
        )
        self.assertEqual(["rogue-physical-id"], apply_index.deleted_point_ids)
        self.assertEqual(1, apply_report.skipped)

    def test_qdrant_scroll_adapter_carries_physical_id_into_delete(self) -> None:
        physical_id = "00000000-0000-0000-0000-000000000059"
        share_id = "00000000-0000-0000-0000-000000000060"

        class FakeQdrantClient:
            def __init__(self) -> None:
                self.delete_calls: list[dict] = []

            def scroll(self, **kwargs):
                return [
                    SimpleNamespace(
                        id=physical_id,
                        payload={
                            "share_id": share_id,
                            "index_version": 4,
                            "content_hash": "b" * 64,
                            "visibility": "PUBLIC",
                        },
                    )
                ], None

            def delete(self, **kwargs):
                self.delete_calls.append(kwargs)

            def retrieve(self, **kwargs):
                return []

        client = FakeQdrantClient()
        index = reconcile.QdrantMaintenanceIndex(
            client=client,
            collection="shared_guide_embeddings_v1",
            dimension=768,
        )
        points, next_offset = index.scroll(
            collection="shared_guide_embeddings_v1",
            offset=None,
            limit=100,
        )

        self.assertIsNone(next_offset)
        self.assertEqual(physical_id, points[0].point_id)
        index.delete_point(
            collection="shared_guide_embeddings_v1",
            point=points[0],
        )
        selector = client.delete_calls[0]["points_selector"]
        self.assertIsInstance(selector, models.FilterSelector)
        conditions = selector.filter.must
        self.assertTrue(
            any(
                isinstance(condition, models.HasIdCondition)
                and physical_id in condition.has_id
                for condition in conditions
            )
        )
        for key, value in (
            ("share_id", share_id),
            ("index_version", 4),
            ("content_hash", "b" * 64),
            ("visibility", "PUBLIC"),
        ):
            self.assertTrue(
                any(
                    isinstance(condition, models.FieldCondition)
                    and condition.key == key
                    and condition.match.value == value
                    for condition in conditions
                )
            )

    def test_collection_schema_mismatch_is_reported_without_mutation(self) -> None:
        valid_client = FakeCollectionClient(
            dimension=768,
            distance=models.Distance.COSINE,
        )
        reindex.validate_existing_collection(
            valid_client,
            "shared_guide_embeddings_v1",
        )

        bad_client = FakeCollectionClient(
            dimension=384,
            distance=models.Distance.DOT,
        )

        with self.assertRaises(reindex.CollectionSchemaMismatchError):
            reindex.validate_existing_collection(
                bad_client,
                "shared_guide_embeddings_v1",
            )
        self.assertEqual(
            ["shared_guide_embeddings_v1"],
            bad_client.collection_exists_calls,
        )
        self.assertEqual(
            ["shared_guide_embeddings_v1"],
            bad_client.get_collection_calls,
        )

        record = make_record("00000000-0000-0000-0000-000000000056")

        def reject_schema() -> None:
            reindex.validate_existing_collection(
                bad_client,
                "shared_guide_embeddings_v1",
            )

        reindex_report = reindex.run_reindex(
            reindex.parse_args([]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([record]),
            text_builder=EmbeddingTextBuilder(),
            embedding_client=FakeEmbeddingClient(),
            vector_index=FakeMaintenanceIndex(),
            validate_schema=reject_schema,
            output=io.StringIO(),
        )
        self.assertTrue(reindex_report.schema_mismatch)
        self.assertEqual(1, reindex_report.exit_code)

        reconcile_index = FakeMaintenanceIndex(
            [
                reconcile.IndexedPoint(
                    point_id=record.share_id,
                    share_id=record.share_id,
                    index_version=record.index_version,
                    content_hash=record.content_hash,
                )
            ]
        )
        reconcile_report = reconcile.run_reconcile(
            reconcile.parse_args(["--apply", "--delete-extra"]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([record]),
            embedding_client=FakeEmbeddingClient(),
            vector_index=reconcile_index,
            validate_schema=reject_schema,
            output=io.StringIO(),
        )
        self.assertTrue(reconcile_report.schema_mismatch)
        self.assertEqual(1, reconcile_report.exit_code)
        self.assertEqual([], reconcile_index.upserts)
        self.assertEqual([], reconcile_index.deleted_point_ids)

    def test_public_ready_rows_missing_timestamps_are_inconsistent_failures(self) -> None:
        missing_published = make_inconsistent_record(
            "00000000-0000-0000-0000-000000000057",
            missing="published_at",
        )
        missing_indexed = make_inconsistent_record(
            "00000000-0000-0000-0000-000000000058",
            missing="indexed_at",
        )
        store = FakeMaintenanceStore([missing_published, missing_indexed])
        reindex_output = io.StringIO()
        reindex_report = reindex.run_reindex(
            reindex.parse_args([]),
            collection="shared_guide_embeddings_v1",
            store=store,
            text_builder=EmbeddingTextBuilder(),
            embedding_client=FakeEmbeddingClient(),
            vector_index=FakeMaintenanceIndex(),
            output=reindex_output,
        )

        self.assertEqual(
            [missing_published.share_id, missing_indexed.share_id],
            reindex_report.inconsistent,
        )
        self.assertEqual(2, reindex_report.failed)
        self.assertIn("outcome=inconsistent", reindex_output.getvalue())

        reconcile_report = reconcile.run_reconcile(
            reconcile.parse_args([]),
            collection="shared_guide_embeddings_v1",
            store=FakeMaintenanceStore([missing_published, missing_indexed]),
            embedding_client=FakeEmbeddingClient(),
            vector_index=FakeMaintenanceIndex(),
            output=io.StringIO(),
        )
        self.assertEqual(
            [missing_published.share_id, missing_indexed.share_id],
            reconcile_report.inconsistent,
        )
        self.assertEqual([], reconcile_report.missing)
        self.assertEqual([], reconcile_report.extra)
        self.assertEqual(2, reconcile_report.failed)

    def test_mysql_maintenance_reader_keeps_incomplete_public_ready_row_visible(self) -> None:
        record = make_record("00000000-0000-0000-0000-000000000063")
        row = {
            "share_id": record.share_id,
            "author_user_id": record.author_user_id,
            "source_session_id": record.source_session_id,
            "source_version_id": record.source_version_id,
            "source_version_number": record.source_version_number,
            "title": record.title,
            "city": record.city,
            "city_normalized": record.city_normalized,
            "travel_days": record.travel_days,
            "transportation": record.transportation,
            "accommodation": record.accommodation,
            "preferences_json": json.dumps(record.preferences),
            "snapshot_json": record.snapshot.model_dump_json(),
            "retrieval_text": record.retrieval_text,
            "content_hash": record.content_hash,
            "quality_level": record.quality_level,
            "quality_score": record.quality_score,
            "publication_status": PublicationStatus.PUBLIC.value,
            "index_status": ShareIndexStatus.READY.value,
            "embedding_model": record.embedding_model,
            "embedding_dimension": record.embedding_dimension,
            "retrieval_template_version": record.retrieval_template_version,
            "index_version": record.index_version,
            "like_count": record.like_count,
            "last_index_error": None,
            "indexed_at": NOW,
            "published_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }

        observed = reindex.MySQLMaintenanceStore._maintenance_record_from_row(row)

        self.assertEqual(record.share_id, observed.share_id)
        self.assertIsNone(observed.published_at)
        self.assertEqual(ShareIndexStatus.READY, observed.index_status)
        self.assertEqual(PublicationStatus.PUBLIC, observed.publication_status)


class RagEvaluationTests(unittest.TestCase):
    def test_frozen_fixture_metrics_are_deterministic_and_include_threshold(self) -> None:
        first = evaluation.evaluate_fixture(FIXTURE_DIR, min_score=0.55)
        second = evaluation.evaluate_fixture(FIXTURE_DIR, min_score=0.55)

        self.assertEqual(first, second)
        self.assertEqual(1.0, first["metrics"]["same_city_public_correctness"])
        self.assertEqual(0.0, first["metrics"]["cancelled_recall"])
        self.assertGreaterEqual(first["metrics"]["recall_at_3"], first["thresholds"]["min_recall_at_3"])
        self.assertGreaterEqual(first["metrics"]["ndcg_at_3"], first["thresholds"]["min_ndcg_at_3"])
        self.assertEqual(0.55, first["rag_min_score"])
        self.assertEqual(
            {"min_ms": 3, "max_ms": 7, "mean_ms": 5.0, "p95_ms": 7},
            first["latency_ms"],
        )
        self.assertTrue(first["passed"])

    def test_fixture_manifest_counts_match_and_cli_emits_stable_json(self) -> None:
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
        corpus = json.loads((FIXTURE_DIR / "corpus.json").read_text(encoding="utf-8"))
        queries = json.loads((FIXTURE_DIR / "queries.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["corpus_count"], len(corpus["documents"]))
        self.assertEqual(manifest["query_count"], len(queries["queries"]))

        outputs = []
        for _ in range(2):
            stream = io.StringIO()
            with patch("sys.stdout", stream):
                exit_code = evaluation.main(
                    ["--fixture-dir", str(FIXTURE_DIR), "--summary-only"]
                )
            self.assertEqual(0, exit_code)
            outputs.append(stream.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertTrue(json.loads(outputs[0])["passed"])

    def test_live_mode_requires_manual_environment_and_never_mutates_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "v1"
            copied.mkdir()
            for name in ("manifest.json", "corpus.json", "queries.json"):
                (copied / name).write_bytes((FIXTURE_DIR / name).read_bytes())
            before = {path.name: path.read_bytes() for path in copied.iterdir()}
            stream = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), patch("sys.stdout", stream):
                exit_code = evaluation.main(
                    [
                        "--fixture-dir",
                        str(copied),
                        "--live-dashscope",
                        "--summary-only",
                    ]
                )
            after = {path.name: path.read_bytes() for path in copied.iterdir()}

        self.assertNotEqual(0, exit_code)
        self.assertEqual(before, after)
        self.assertIn("DASHSCOPE_API_KEY", stream.getvalue())
        self.assertIn("DASHSCOPE_BASE_URL", stream.getvalue())

    def test_evaluation_exits_nonzero_when_locked_quality_thresholds_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "v1"
            copied.mkdir()
            for name in ("manifest.json", "corpus.json", "queries.json"):
                (copied / name).write_bytes((FIXTURE_DIR / name).read_bytes())
            queries = json.loads((copied / "queries.json").read_text(encoding="utf-8"))
            for query in queries["queries"]:
                for relevant_id in query["expected_relevant_share_ids"]:
                    query["scores"][relevant_id] = 0.1
            (copied / "queries.json").write_text(
                json.dumps(queries, ensure_ascii=False),
                encoding="utf-8",
            )
            stream = io.StringIO()
            with patch("sys.stdout", stream):
                exit_code = evaluation.main(
                    ["--fixture-dir", str(copied), "--summary-only"]
                )

        self.assertEqual(1, exit_code)
        report = json.loads(stream.getvalue())
        self.assertFalse(report["passed"])
        self.assertLess(
            report["metrics"]["recall_at_3"],
            report["thresholds"]["min_recall_at_3"],
        )


if __name__ == "__main__":
    unittest.main()
