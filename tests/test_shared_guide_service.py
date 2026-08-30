from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

from app.observability.rag_metrics import RagMetrics
from app.persistence.exceptions import DraftConflictError, SessionNotFoundError
from app.rag.text_builder import EmbeddingTextBuilder
from app.schemas.trip_schema import TripPlan, TripRequest
from app.sharing.exceptions import (
    SharedGuideConflictError,
    SharedGuideForbiddenError,
    SharedGuideNotFoundError,
    SharedGuideUnavailableError,
)
from app.sharing.models import (
    IndexJobStatus,
    IndexOperation,
    LikeMutation,
    OwnedSharedGuidePage,
    PublicationStatus,
    ShareIndexIntent,
    ShareIndexJob,
    ShareIndexStatus,
    SharedGuidePage,
    SharedGuideRecord,
)
from app.sharing.service import SharedGuideService


UTC = timezone.utc
NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def metric_sample_value(metrics: RagMetrics, name: str, labels: dict[str, str] | None = None):
    labels = labels or {}
    for family in metrics.collect():
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None


def make_plan(*, marker: str = "original") -> TripPlan:
    return TripPlan.model_validate(
        {
            "city": "杭州市",
            "start_date": "2026-09-01",
            "end_date": "2026-09-02",
            "days": [
                {
                    "date": "2026-09-01",
                    "day_index": 0,
                    "description": f"西湖游览-{marker}",
                    "transportation": "地铁",
                    "accommodation": "经济型酒店",
                    "attractions": [
                        {
                            "name": "西湖",
                            "address": "西湖风景区",
                            "location": {"longitude": 120.15, "latitude": 30.25},
                            "visit_duration": 120,
                            "description": "湖景",
                            "poi_id": "private-poi-id",
                            "image_url": "https://images.example/west-lake.jpg",
                            "ticket_price": 99,
                        }
                    ],
                    "meals": [],
                },
                {
                    "date": "2026-09-02",
                    "day_index": 1,
                    "description": "灵隐寺游览",
                    "transportation": "公交",
                    "accommodation": "经济型酒店",
                    "attractions": [],
                    "meals": [],
                },
            ],
            "weather_info": [{"date": "2026-09-01", "day_weather": "晴"}],
            "overall_suggestions": "错峰出行",
            "budget": {"total": 9999},
        }
    )


def make_request() -> TripRequest:
    return TripRequest(
        city="杭州市",
        start_date="2026-09-01",
        end_date="2026-09-02",
        travel_days=2,
        transportation="地铁",
        accommodation="经济型酒店",
        preferences=["自然风光", "美食"],
        free_text_input="private request text",
    )


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float = 1) -> None:
        self.current += timedelta(seconds=seconds)


class FakeStateStore:
    def __init__(self, state) -> None:
        self.state = state
        self.calls: list[tuple[str, str | None]] = []

    def get_state(self, session_id: str, *, user_id: str | None = None):
        self.calls.append((session_id, user_id))
        if session_id != self.state.session_id or user_id != self.state.user_id:
            raise SessionNotFoundError(session_id)
        return self.state


class FakeVersion:
    def __init__(self, plan: TripPlan, *, number: int = 1) -> None:
        self.version_id = f"version-{number}"
        self.version_number = number
        self.trip_plan = plan

    def quality_snapshot(self):
        return SimpleNamespace(quality_level="excellent", quality_score=92.5)


class FakeTripDraftService:
    def __init__(self, version: FakeVersion) -> None:
        self.version = version
        self.calls = []
        self.error: Exception | None = None

    def ensure_original_version(self, state):
        self.calls.append(state)
        if self.error is not None:
            raise self.error
        return self.version


class FakeEmbeddingClient:
    model = "qwen3.7-text-embedding"
    dimension = 3

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.vector = [0.1, 0.2, 0.3]

    def embed(self, text: str) -> list[float]:
        self.events.append("embed")
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return list(self.vector)


class FakeVectorIndex:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.upsert_calls = []
        self.delete_calls = []
        self.upsert_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.on_upsert = None
        self.on_delete = None

    def upsert(self, share_id: str, vector, *, payload) -> None:
        self.events.append("upsert")
        self.upsert_calls.append((share_id, list(vector), dict(payload)))
        callback, self.on_upsert = self.on_upsert, None
        if callback is not None:
            callback()
        if self.upsert_error is not None:
            raise self.upsert_error

    def delete(self, share_id: str, *, index_version: int) -> None:
        self.events.append("delete")
        self.delete_calls.append((share_id, index_version))
        callback, self.on_delete = self.on_delete, None
        if callback is not None:
            callback()
        if self.delete_error is not None:
            raise self.delete_error


class FakeSharedGuideStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.records: dict[str, SharedGuideRecord] = {}
        self.by_session: dict[tuple[str, str], str] = {}
        self.jobs: dict[str, ShareIndexJob] = {}
        self.next_share = 1
        self.next_job = 1
        self.claim_enabled = True
        self.on_claim_miss = None
        self.before_persist = None
        self.persisted_retrieval_text = None
        self.persisted_content_hash = None
        self.drafts = []
        self.list_public_result = SharedGuidePage()
        self.list_owned_result = OwnedSharedGuidePage()
        self.public_detail = SimpleNamespace(share_id="public-detail")
        self.last_public_query = None
        self.last_public_viewer = None
        self.last_owned_query = None
        self.last_owned_author = None
        self.like_calls = []
        self.unlike_calls = []
        self.claim_worker_ids = []
        self.failure_calls = []
        self.superseded_job_ids = []

    def _new_job(self, record: SharedGuideRecord, operation: IndexOperation, now: datetime):
        job = ShareIndexJob(
            job_id=f"job-{self.next_job}",
            share_id=record.share_id,
            operation=operation,
            index_version=record.index_version,
            created_at=now,
            updated_at=now,
        )
        self.next_job += 1
        self.jobs[job.job_id] = job
        return job

    def _new_record(self, draft, now: datetime) -> SharedGuideRecord:
        share_id = f"00000000-0000-0000-0000-{self.next_share:012d}"
        self.next_share += 1
        values = draft.model_dump()
        if self.persisted_retrieval_text is not None:
            values["retrieval_text"] = self.persisted_retrieval_text
        if self.persisted_content_hash is not None:
            values["content_hash"] = self.persisted_content_hash
        return SharedGuideRecord(
            **values,
            share_id=share_id,
            publication_status=PublicationStatus.PUBLISHING,
            index_status=ShareIndexStatus.PENDING,
            index_version=1,
            published_at=now,
            created_at=now,
            updated_at=now,
        )

    def create_publish_intent(self, draft, *, now: datetime) -> ShareIndexIntent:
        self.events.append("stage_create")
        if self.before_persist is not None:
            self.before_persist(draft)
        self.drafts.append(draft.model_copy(deep=True))
        key = (draft.author_user_id, draft.source_session_id)
        share_id = self.by_session.get(key)
        if share_id is None:
            record = self._new_record(draft, now)
            self.records[record.share_id] = record
            self.by_session[key] = record.share_id
            job = self._new_job(record, IndexOperation.UPSERT, now)
            return ShareIndexIntent(record=record, job=job, created=True, operation_required=True)

        record = self.records[share_id]
        if record.publication_status is PublicationStatus.UNPUBLISHED:
            record = SharedGuideRecord(
                **draft.model_dump(),
                share_id=record.share_id,
                publication_status=PublicationStatus.PUBLISHING,
                index_status=ShareIndexStatus.PENDING,
                index_version=record.index_version + 1,
                like_count=record.like_count,
                published_at=now,
                created_at=record.created_at,
                updated_at=now,
            )
            self.records[share_id] = record
            job = self._new_job(record, IndexOperation.UPSERT, now)
            return ShareIndexIntent(record=record, job=job, created=False, operation_required=True)
        if record.content_hash != draft.content_hash:
            raise SharedGuideConflictError("changed content requires update")
        if (
            record.publication_status is PublicationStatus.PUBLIC
            and record.index_status is ShareIndexStatus.READY
        ):
            return ShareIndexIntent(
                record=record,
                job=None,
                created=False,
                operation_required=False,
            )
        job = next(
            (
                item
                for item in self.jobs.values()
                if item.share_id == share_id
                and item.index_version == record.index_version
                and item.operation is IndexOperation.UPSERT
            ),
            None,
        )
        return ShareIndexIntent(
            record=record,
            job=job,
            created=False,
            operation_required=(
                job is not None
                and job.status in (IndexJobStatus.PENDING, IndexJobStatus.RUNNING)
            ),
        )

    def stage_update(
        self,
        share_id: str,
        author_user_id: str,
        draft,
        *,
        now: datetime,
        allow_active_upsert_supersede: bool = False,
    ) -> ShareIndexIntent:
        self.events.append("stage_update")
        record = self.get_owned(share_id, author_user_id)
        if record.publication_status is PublicationStatus.UNPUBLISHED:
            raise SharedGuideConflictError("use the session share endpoint")
        if record.source_session_id != draft.source_session_id:
            raise SharedGuideConflictError("session mismatch")
        active_upsert = next(
            (
                job
                for job in self.jobs.values()
                if job.share_id == share_id
                and job.operation is IndexOperation.UPSERT
                and job.status is IndexJobStatus.RUNNING
            ),
            None,
        )
        if active_upsert is not None and not allow_active_upsert_supersede:
            raise SharedGuideConflictError("active upsert lease must be allowed explicitly")
        if self.before_persist is not None:
            self.before_persist(draft)
        updated = SharedGuideRecord(
            **draft.model_dump(),
            share_id=record.share_id,
            publication_status=PublicationStatus.PUBLISHING,
            index_status=ShareIndexStatus.PENDING,
            index_version=record.index_version + 1,
            like_count=record.like_count,
            published_at=now,
            created_at=record.created_at,
            updated_at=now,
        )
        self.records[share_id] = updated
        self.drafts.append(draft.model_copy(deep=True))
        job = self._new_job(updated, IndexOperation.UPSERT, now)
        return ShareIndexIntent(record=updated, job=job, created=False, operation_required=True)

    def stage_unpublish(
        self,
        share_id: str,
        author_user_id: str,
        *,
        now: datetime,
    ) -> ShareIndexIntent:
        self.events.append("stage_unpublish")
        record = self.get_owned(share_id, author_user_id)
        if record.publication_status is PublicationStatus.UNPUBLISHED:
            return ShareIndexIntent(
                record=record,
                job=None,
                created=False,
                operation_required=False,
            )
        hidden = record.model_copy(
            update={
                "publication_status": PublicationStatus.UNPUBLISHED,
                "index_status": ShareIndexStatus.DELETE_PENDING,
                "last_index_error": None,
                "updated_at": now,
            }
        )
        self.records[share_id] = hidden
        job = self._new_job(hidden, IndexOperation.DELETE, now)
        return ShareIndexIntent(record=hidden, job=job, created=False, operation_required=True)

    def claim_index_job(self, job_id: str, worker_id: str, *, now: datetime, lease_seconds: float):
        self.events.append("claim")
        self.claim_worker_ids.append(worker_id)
        if not self.claim_enabled:
            if self.on_claim_miss is not None:
                self.on_claim_miss()
            return None
        job = self.jobs[job_id]
        if job.status is not IndexJobStatus.PENDING or (
            job.next_retry_at is not None and job.next_retry_at > now
        ):
            return None
        claimed = job.model_copy(
            update={
                "status": IndexJobStatus.RUNNING,
                "attempt_count": job.attempt_count + 1,
                "next_retry_at": None,
                "lease_owner": worker_id,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        )
        self.jobs[job_id] = claimed
        return claimed

    def complete_index_operation(
        self,
        job_id,
        share_id,
        index_version,
        operation,
        *,
        worker_id,
        now,
    ):
        self.events.append("complete")
        record = self.records[share_id]
        job = self.jobs[job_id]
        if (
            record.index_version != index_version
            or job.status is not IndexJobStatus.RUNNING
            or job.lease_owner != worker_id
            or job.index_version != index_version
            or job.operation is not operation
        ):
            return False
        if operation is IndexOperation.UPSERT:
            if record.publication_status is not PublicationStatus.PUBLISHING:
                return False
            record = record.model_copy(
                update={
                    "publication_status": PublicationStatus.PUBLIC,
                    "index_status": ShareIndexStatus.READY,
                    "indexed_at": now,
                    "last_index_error": None,
                    "updated_at": now,
                }
            )
        else:
            if record.publication_status is not PublicationStatus.UNPUBLISHED:
                return False
            record = record.model_copy(
                update={
                    "index_status": ShareIndexStatus.DELETED,
                    "last_index_error": None,
                    "updated_at": now,
                }
            )
        self.records[share_id] = record
        self.jobs[job_id] = job.model_copy(
            update={
                "status": IndexJobStatus.SUCCEEDED,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        )
        return True

    def record_index_failure(
        self,
        job_id,
        share_id,
        index_version,
        operation,
        error,
        *,
        worker_id,
        next_retry_at,
        terminal,
        now,
    ):
        self.events.append("failure")
        self.failure_calls.append(
            (job_id, share_id, index_version, operation, error, worker_id)
        )
        record = self.records[share_id]
        job = self.jobs[job_id]
        if record.index_version != index_version or job.lease_owner != worker_id:
            return False
        next_status = (
            ShareIndexStatus.FAILED
            if operation is IndexOperation.UPSERT or terminal
            else ShareIndexStatus.DELETE_PENDING
        )
        error_class = type(error).__name__ if isinstance(error, BaseException) else "IndexError"
        self.records[share_id] = record.model_copy(
            update={
                "index_status": next_status,
                "last_index_error": error_class,
                "updated_at": now,
            }
        )
        self.jobs[job_id] = job.model_copy(
            update={
                "status": IndexJobStatus.FAILED if terminal else IndexJobStatus.PENDING,
                "next_retry_at": None if terminal else next_retry_at,
                "lease_owner": None,
                "lease_expires_at": None,
                "last_error": error_class,
                "updated_at": now,
            }
        )
        return True

    def supersede_index_job(self, job_id: str, *, worker_id: str | None = None, now: datetime):
        self.events.append("supersede")
        job = self.jobs[job_id]
        record = self.records[job.share_id]
        if worker_id is not None and job.lease_owner != worker_id:
            return False
        if record.index_version <= job.index_version:
            return False
        self.superseded_job_ids.append(job_id)
        self.jobs[job_id] = job.model_copy(
            update={
                "status": IndexJobStatus.SUCCEEDED,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
        )
        return True

    def get_owned(self, share_id: str, author_user_id: str) -> SharedGuideRecord:
        record = self.records.get(share_id)
        if record is None or record.author_user_id != author_user_id:
            raise SharedGuideNotFoundError(share_id)
        return record

    def get_for_author_session(self, author_user_id: str, source_session_id: str):
        share_id = self.by_session.get((author_user_id, source_session_id))
        return self.records.get(share_id) if share_id is not None else None

    def list_public(self, query, viewer_user_id=None):
        self.last_public_query = query
        self.last_public_viewer = viewer_user_id
        return self.list_public_result

    def get_public(self, share_id, viewer_user_id=None):
        self.last_public_viewer = viewer_user_id
        return self.public_detail

    def list_owned(self, author_user_id, query):
        self.last_owned_author = author_user_id
        self.last_owned_query = query
        return self.list_owned_result

    def put_like(self, share_id, user_id, *, now):
        self.like_calls.append((share_id, user_id, now))
        return LikeMutation(liked=True, like_count=1)

    def delete_like(self, share_id, user_id):
        self.unlike_calls.append((share_id, user_id))
        return LikeMutation(liked=False, like_count=0)


class SharedGuideServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        self.clock = FakeClock()
        self.request = make_request()
        self.state = SimpleNamespace(
            session_id="session-1",
            user_id="user-1",
            status="completed",
            request=self.request,
        )
        self.state_store = FakeStateStore(self.state)
        self.version = FakeVersion(make_plan())
        self.trip_draft_service = FakeTripDraftService(self.version)
        self.store = FakeSharedGuideStore(self.events)
        self.embedding = FakeEmbeddingClient(self.events)
        self.index = FakeVectorIndex(self.events)
        self.service = self.make_service()

    def make_service(
        self,
        *,
        write_enabled: bool = True,
        metrics: RagMetrics | None = None,
    ) -> SharedGuideService:
        service_kwargs = {
            "state_store": self.state_store,
            "trip_draft_service": self.trip_draft_service,
            "store": self.store,
            "text_builder": EmbeddingTextBuilder(),
            "embedding_client": self.embedding,
            "vector_index": self.index,
            "write_enabled": write_enabled,
            "lease_seconds": 30,
            "max_attempts": 3,
            "retry_base_seconds": 2,
            "retry_max_seconds": 10,
            "clock": self.clock,
        }
        if metrics is not None:
            service_kwargs["metrics"] = metrics
        return SharedGuideService(**service_kwargs)

    def publish(self, *, title: str | None = None):
        return self.service.share_session("session-1", "user-1", title=title)

    def test_share_requires_authenticated_owner_and_completed_state(self):
        with self.assertRaises(SharedGuideForbiddenError):
            self.service.share_session("session-1", "", title=None)
        self.assertEqual(self.state_store.calls, [])

        with self.assertRaises(SessionNotFoundError):
            self.service.share_session("session-1", "user-2", title=None)
        self.assertEqual(self.state_store.calls[-1], ("session-1", "user-2"))

        self.state.status = "running"
        with self.assertRaises(SharedGuideConflictError):
            self.publish()
        self.assertEqual(self.trip_draft_service.calls, [])

    def test_share_maps_incomplete_original_version_to_conflict(self):
        self.trip_draft_service.error = DraftConflictError("missing evaluation")
        with self.assertRaises(SharedGuideConflictError):
            self.publish()
        self.assertEqual(self.embedding.calls, [])

    def test_snapshot_is_private_bounded_deep_copied_and_title_is_normalized(self):
        def mutate_source_before_persistence(draft):
            self.version.trip_plan.days[0].description = "mutated before persistence"

        self.store.before_persist = mutate_source_before_persistence
        self.store.persisted_retrieval_text = "persisted retrieval sentinel"
        self.store.persisted_content_hash = "b" * 64
        result = self.publish(title="  <b>Ｈａｎｇｚｈｏｕ</b>\u0000 攻略  ")
        draft = self.store.drafts[0]

        self.assertEqual(result.title, "Hangzhou 攻略")
        self.assertEqual(draft.snapshot.request.model_dump(), {
            "city": "杭州市",
            "travel_days": 2,
            "transportation": "地铁",
            "accommodation": "经济型酒店",
            "preferences": ["自然风光", "美食"],
        })
        self.assertNotIn("private request text", draft.model_dump_json())
        self.assertEqual(draft.city_normalized, "杭州")
        self.assertEqual(draft.transportation, "公共交通")
        self.assertEqual(draft.quality_level, "excellent")
        self.assertEqual(draft.quality_score, 92.5)
        self.assertEqual(draft.snapshot.trip_plan.days[0].description, "西湖游览-original")
        self.assertEqual(result.retrieval_text, "persisted retrieval sentinel")
        self.assertEqual(result.content_hash, "b" * 64)
        self.assertEqual(self.embedding.calls, ["persisted retrieval sentinel"])

    def test_missing_or_stripped_title_uses_deterministic_default_and_enforces_limit(self):
        result = self.publish(title="<b></b>\u0000")
        self.assertEqual(result.title, "杭州2日旅行攻略")

        other = self.make_service()
        with self.assertRaises(ValueError):
            other.share_session("session-1", "user-1", title="x" * 201)

    def test_optional_title_can_be_omitted_from_share_and_update_calls(self):
        record = self.service.share_session("session-1", "user-1")
        self.trip_draft_service.version = FakeVersion(make_plan(marker="v2"), number=2)
        updated = self.service.update(record.share_id, "user-1")
        self.assertEqual(record.title, "杭州2日旅行攻略")
        self.assertEqual(updated.title, "杭州2日旅行攻略")

    def test_success_stages_claims_then_embeds_persisted_text_upserts_and_completes(self):
        result = self.publish()

        self.assertEqual(self.events, ["stage_create", "claim", "embed", "upsert", "complete"])
        self.assertTrue(self.store.claim_worker_ids[0].startswith("sync:"))
        UUID(self.store.claim_worker_ids[0].removeprefix("sync:"))
        self.assertEqual(
            self.embedding.calls,
            [self.store.records[result.share_id].retrieval_text],
        )
        self.assertEqual(len(self.index.upsert_calls), 1)
        share_id, vector, payload = self.index.upsert_calls[0]
        UUID(share_id)
        self.assertEqual(vector, [0.1, 0.2, 0.3])
        self.assertEqual(
            set(payload),
            {
                "share_id",
                "city",
                "travel_days",
                "transportation",
                "visibility",
                "quality_score",
                "published_at",
                "index_version",
                "content_hash",
            },
        )
        self.assertEqual(payload["visibility"], "PUBLIC")
        self.assertEqual(result.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(result.index_status, ShareIndexStatus.READY)

    def test_metrics_record_share_publication_success_and_duration(self):
        metrics = RagMetrics()
        self.service = self.make_service(metrics=metrics)

        before = metric_sample_value(
            metrics,
            "travel_agent_share_publications_total",
            {"stage": "upsert", "outcome": "success"},
        ) or 0.0
        before_duration = metric_sample_value(
            metrics,
            "travel_agent_share_publication_duration_seconds_count",
            {"outcome": "success"},
        ) or 0.0

        self.publish()

        self.assertEqual(
            1.0,
            metric_sample_value(
                metrics,
                "travel_agent_share_publications_total",
                {"stage": "upsert", "outcome": "success"},
            )
            - before,
        )
        self.assertEqual(
            1.0,
            metric_sample_value(
                metrics,
                "travel_agent_share_publication_duration_seconds_count",
                {"outcome": "success"},
            )
            - before_duration,
        )

    def test_metrics_record_share_publication_failure(self):
        metrics = RagMetrics()
        self.service = self.make_service(metrics=metrics)
        self.embedding.error = RuntimeError("provider details must not be a label")

        with self.assertRaises(SharedGuideUnavailableError):
            self.publish()

        self.assertEqual(
            1.0,
            metric_sample_value(
                metrics,
                "travel_agent_share_publications_total",
                {"stage": "upsert", "outcome": "unavailable"},
            ),
        )

    def test_identical_post_is_idempotent_but_changed_source_requires_update(self):
        first = self.publish()
        second = self.publish()
        self.assertEqual(second.share_id, first.share_id)
        self.assertEqual(len(self.embedding.calls), 1)

        self.version = FakeVersion(make_plan(marker="changed"), number=2)
        self.trip_draft_service.version = self.version
        with self.assertRaises(SharedGuideConflictError):
            self.publish()
        self.assertEqual(len(self.embedding.calls), 1)

    def test_post_after_unpublish_reuses_identity_and_increments_version(self):
        first = self.publish()
        first_published_at = first.published_at
        self.clock.advance()
        self.service.unpublish(first.share_id, "user-1")
        self.clock.advance()
        republished = self.publish()

        self.assertEqual(republished.share_id, first.share_id)
        self.assertEqual(republished.index_version, 2)
        self.assertGreater(republished.published_at, first_published_at)
        self.assertEqual(len(self.embedding.calls), 2)

    def test_update_uses_latest_version_and_preserves_identity_likes_and_creation(self):
        original = self.publish()
        liked = original.model_copy(update={"like_count": 7})
        self.store.records[original.share_id] = liked
        self.trip_draft_service.version = FakeVersion(make_plan(marker="v2"), number=2)
        observed = {}
        self.index.on_upsert = lambda: observed.update(
            record=self.store.records[original.share_id]
        )
        self.clock.advance()

        updated = self.service.update(original.share_id, "user-1", title="新版")

        staged = observed["record"]
        self.assertEqual(staged.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(staged.index_status, ShareIndexStatus.PENDING)
        self.assertIsNone(staged.indexed_at)
        self.assertEqual(updated.share_id, original.share_id)
        self.assertEqual(updated.created_at, original.created_at)
        self.assertEqual(updated.like_count, 7)
        self.assertEqual(updated.source_version_number, 2)
        self.assertEqual(updated.index_version, 2)
        self.assertGreater(updated.published_at, original.published_at)

    def test_update_can_supersede_active_upsert_through_explicit_service_capability(self):
        def update_during_old_upsert():
            original = next(iter(self.store.records.values()))
            self.service.update(original.share_id, "user-1", title="新版本")

        self.index.on_upsert = update_during_old_upsert
        with self.assertRaises(SharedGuideConflictError):
            self.publish()

        original = next(iter(self.store.records.values()))
        old_job_id = next(
            job_id
            for job_id, job in self.store.jobs.items()
            if job.share_id == original.share_id
            and job.operation is IndexOperation.UPSERT
            and job.index_version == 1
        )
        current = self.store.get_owned(original.share_id, "user-1")
        self.assertEqual(current.index_version, 2)
        self.assertEqual(current.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(current.index_status, ShareIndexStatus.READY)
        self.assertEqual(self.store.jobs[old_job_id].status, IndexJobStatus.SUCCEEDED)
        self.assertIn((original.share_id, 1), self.index.delete_calls)
        self.assertNotIn((original.share_id, 2), self.index.delete_calls)

    def test_update_unpublished_conflicts_and_cross_user_mutations_are_not_found(self):
        record = self.publish()
        self.service.unpublish(record.share_id, "user-1")
        with self.assertRaises(SharedGuideConflictError):
            self.service.update(record.share_id, "user-1", title=None)
        with self.assertRaises(SharedGuideNotFoundError):
            self.service.update(record.share_id, "user-2", title=None)
        with self.assertRaises(SharedGuideNotFoundError):
            self.service.unpublish(record.share_id, "user-2")

    def test_unpublish_claim_delete_and_complete_are_ordered_exactly(self):
        record = self.publish()
        self.events.clear()

        result = self.service.unpublish(record.share_id, "user-1")

        self.assertEqual(
            self.events,
            ["stage_unpublish", "claim", "delete", "complete"],
        )
        self.assertEqual(result.index_status, ShareIndexStatus.DELETED)

    def test_unpublish_claim_miss_does_not_call_delete_or_provider(self):
        record = self.publish()
        self.events.clear()
        self.store.claim_enabled = False
        embedding_calls = len(self.embedding.calls)

        result = self.service.unpublish(record.share_id, "user-1")

        self.assertEqual(result.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(result.index_status, ShareIndexStatus.DELETE_PENDING)
        self.assertEqual(len(self.embedding.calls), embedding_calls)
        self.assertEqual(self.index.delete_calls, [])
        self.assertNotIn("delete", self.events)

    def test_stale_unpublish_completion_supersedes_only_the_obsolete_delete_job(self):
        record = self.publish()

        def republish_during_delete():
            self.service.share_session("session-1", "user-1")

        self.index.on_delete = republish_during_delete
        result = self.service.unpublish(record.share_id, "user-1")

        delete_job = next(
            job_id
            for job_id, job in self.store.jobs.items()
            if job.operation is IndexOperation.DELETE
        )
        newer_job = next(
            job_id
            for job_id, job in self.store.jobs.items()
            if job.operation is IndexOperation.UPSERT and job.index_version == 2
        )
        self.assertEqual(result.index_version, 2)
        self.assertEqual(result.publication_status, PublicationStatus.PUBLIC)
        self.assertEqual(self.store.superseded_job_ids, [delete_job])
        self.assertEqual(self.store.jobs[delete_job].status, IndexJobStatus.SUCCEEDED)
        self.assertEqual(self.store.jobs[newer_job].status, IndexJobStatus.SUCCEEDED)
        self.assertEqual(self.index.delete_calls, [(record.share_id, 1)])

    def test_unpublish_failure_passes_exact_job_share_version_operation_and_sanitizes_error(self):
        record = self.publish()
        self.index.delete_error = RuntimeError("provider secret and response body")
        self.events.clear()

        result = self.service.unpublish(record.share_id, "user-1")

        job = next(
            job
            for job in self.store.jobs.values()
            if job.operation is IndexOperation.DELETE
        )
        failure = self.store.failure_calls[-1]
        self.assertEqual(failure[:4], (job.job_id, record.share_id, 1, IndexOperation.DELETE))
        self.assertIsInstance(failure[4], RuntimeError)
        self.assertTrue(failure[5].startswith("sync:"))
        self.assertEqual(result.index_status, ShareIndexStatus.DELETE_PENDING)
        self.assertEqual(result.last_index_error, "RuntimeError")
        self.assertNotIn("provider secret", result.last_index_error)

    def test_missing_lease_never_calls_providers_and_only_returns_concurrent_ready_record(self):
        self.store.claim_enabled = False
        with self.assertRaises(SharedGuideConflictError):
            self.publish()
        self.assertEqual(self.embedding.calls, [])
        self.assertEqual(self.index.upsert_calls, [])

        self.store = FakeSharedGuideStore(self.events)
        self.service = self.make_service()
        self.store.claim_enabled = False

        def complete_elsewhere():
            record = next(iter(self.store.records.values()))
            self.store.records[record.share_id] = record.model_copy(
                update={
                    "publication_status": PublicationStatus.PUBLIC,
                    "index_status": ShareIndexStatus.READY,
                    "indexed_at": self.clock(),
                }
            )

        self.store.on_claim_miss = complete_elsewhere
        ready = self.publish()
        self.assertEqual(ready.index_status, ShareIndexStatus.READY)
        self.assertEqual(self.embedding.calls, [])

    def test_stale_upsert_cleans_old_version_without_reexposing_unpublished(self):
        def unpublish_during_upsert():
            record = next(iter(self.store.records.values()))
            self.service.unpublish(record.share_id, "user-1")

        self.index.on_upsert = unpublish_during_upsert
        with self.assertRaises(SharedGuideConflictError):
            self.publish()

        record = next(iter(self.store.records.values()))
        self.assertEqual(record.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertNotEqual(record.index_status, ShareIndexStatus.READY)
        self.assertGreaterEqual(self.index.delete_calls.count((record.share_id, 1)), 1)

    def test_stale_upsert_cleanup_is_version_filtered_when_a_newer_update_wins(self):
        def install_newer_version():
            record = next(iter(self.store.records.values()))
            newer = record.model_copy(
                update={
                    "index_version": 2,
                    "content_hash": "b" * 64,
                    "publication_status": PublicationStatus.PUBLISHING,
                    "index_status": ShareIndexStatus.PENDING,
                }
            )
            self.store.records[record.share_id] = newer

        self.index.on_upsert = install_newer_version
        with self.assertRaises(SharedGuideConflictError):
            self.publish()

        record = next(iter(self.store.records.values()))
        self.assertEqual(record.index_version, 2)
        self.assertEqual(record.publication_status, PublicationStatus.PUBLISHING)
        self.assertIn((record.share_id, 1), self.index.delete_calls)
        self.assertNotIn((record.share_id, 2), self.index.delete_calls)

    def test_provider_failure_records_exact_retry_and_raises_sanitized_error(self):
        for failure_at in ("embedding", "qdrant"):
            with self.subTest(failure_at=failure_at):
                self.setUp()
                secret = "secret provider credential"
                if failure_at == "embedding":
                    self.embedding.error = RuntimeError(secret)
                else:
                    self.index.upsert_error = RuntimeError(secret)
                with self.assertRaisesRegex(
                    SharedGuideUnavailableError,
                    "temporarily unavailable",
                ) as raised:
                    self.publish()
                self.assertNotIn(secret, str(raised.exception))
                record = next(iter(self.store.records.values()))
                job = next(iter(self.store.jobs.values()))
                self.assertEqual(record.index_status, ShareIndexStatus.FAILED)
                self.assertEqual(job.status, IndexJobStatus.PENDING)
                self.assertEqual(job.next_retry_at, NOW + timedelta(seconds=2))
                self.assertNotIn(secret, record.last_index_error)

    def test_invalid_embedding_is_failed_before_qdrant_upsert(self):
        self.embedding.vector = [0.1, float("nan"), 0.3]
        with self.assertRaises(SharedGuideUnavailableError):
            self.publish()
        self.assertEqual(self.index.upsert_calls, [])
        record = next(iter(self.store.records.values()))
        self.assertEqual(record.index_status, ShareIndexStatus.FAILED)

    def test_terminal_failed_intent_never_returns_as_a_successful_write(self):
        self.service.max_attempts = 1
        self.embedding.error = RuntimeError("provider down")
        with self.assertRaises(SharedGuideUnavailableError):
            self.publish()
        self.embedding.error = None

        with self.assertRaises(SharedGuideUnavailableError):
            self.publish()
        self.assertEqual(len(self.embedding.calls), 1)
        record = next(iter(self.store.records.values()))
        self.assertEqual(record.publication_status, PublicationStatus.PUBLISHING)
        self.assertEqual(record.index_status, ShareIndexStatus.FAILED)

    def test_unpublish_hides_before_delete_and_delete_failure_is_success_with_compensation(self):
        record = self.publish()
        observed = {}

        def failing_delete(share_id: str, *, index_version: int):
            observed["record"] = self.store.records[share_id]
            self.index.delete_calls.append((share_id, index_version))
            raise RuntimeError("secret qdrant failure")

        self.index.delete = failing_delete
        self.clock.advance()
        result = self.service.unpublish(record.share_id, "user-1")

        self.assertEqual(observed["record"].publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(result.publication_status, PublicationStatus.UNPUBLISHED)
        self.assertEqual(result.index_status, ShareIndexStatus.DELETE_PENDING)
        job = list(self.store.jobs.values())[-1]
        self.assertEqual(job.status, IndexJobStatus.PENDING)
        self.assertIsNotNone(job.next_retry_at)

    def test_repeated_unpublish_is_successful_noop_without_new_delete_job(self):
        record = self.publish()
        first = self.service.unpublish(record.share_id, "user-1")
        job_count = len(self.store.jobs)
        second = self.service.unpublish(record.share_id, "user-1")
        self.assertEqual(first.index_status, ShareIndexStatus.DELETED)
        self.assertEqual(second.index_status, ShareIndexStatus.DELETED)
        self.assertEqual(len(self.store.jobs), job_count)

    def test_read_facades_normalize_filters_and_work_when_writes_are_disabled(self):
        service = self.make_service(write_enabled=False)
        page = service.list_public(
            city="杭州市",
            travel_days=2,
            transportation="公交",
            sort="popular",
            limit=10,
            cursor="opaque",
            viewer_user_id="viewer-1",
        )
        detail = service.get_public("share-1", viewer_user_id="viewer-1")
        owned = service.list_owned("user-1", city="杭州市", transportation="驾车")

        self.assertIs(page, self.store.list_public_result)
        self.assertIs(detail, self.store.public_detail)
        self.assertIs(owned, self.store.list_owned_result)
        self.assertEqual(self.store.last_public_query.city_normalized, "杭州")
        self.assertEqual(self.store.last_public_query.transportation, "公共交通")
        self.assertEqual(self.store.last_public_viewer, "viewer-1")
        self.assertEqual(self.store.last_owned_query.transportation, "自驾")
        self.assertEqual(self.store.last_owned_author, "user-1")

    def test_write_flag_blocks_publish_update_unpublish_like_and_unlike_only(self):
        record = self.publish()
        disabled = self.make_service(write_enabled=False)
        operations = (
            lambda: disabled.share_session("session-1", "user-1", title=None),
            lambda: disabled.update(record.share_id, "user-1", title=None),
            lambda: disabled.unpublish(record.share_id, "user-1"),
            lambda: disabled.like(record.share_id, "viewer-1"),
            lambda: disabled.unlike(record.share_id, "viewer-1"),
        )
        for operation in operations:
            with self.assertRaises(SharedGuideUnavailableError):
                operation()

        self.assertEqual(self.store.like_calls, [])
        self.assertEqual(self.store.unlike_calls, [])

    def test_like_facades_require_caller_and_delegate_atomic_store_operations(self):
        liked = self.service.like("share-1", "viewer-1")
        unliked = self.service.unlike("share-1", "viewer-1")
        self.assertEqual(liked, LikeMutation(liked=True, like_count=1))
        self.assertEqual(unliked, LikeMutation(liked=False, like_count=0))
        self.assertEqual(self.store.like_calls, [("share-1", "viewer-1", NOW)])
        self.assertEqual(self.store.unlike_calls, [("share-1", "viewer-1")])
        with self.assertRaises(SharedGuideForbiddenError):
            self.service.like("share-1", "")


if __name__ == "__main__":
    unittest.main()
