"""Backend-independent shared-guide persistence contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence, TYPE_CHECKING, runtime_checkable

from app.sharing.models import (
    IndexOperation,
    LikeMutation,
    OwnedSharedGuidePage,
    ShareIndexIntent,
    ShareIndexJob,
    SharePublishDraft,
    SharedGuideListQuery,
    SharedGuidePage,
    SharedGuidePublicDetail,
    SharedGuideRecord,
)

if TYPE_CHECKING:
    from app.rag.models import IndexedIdentity


@dataclass(frozen=True)
class ShareIndexBacklog:
    """Low-cardinality durable job counts for health and metrics."""

    pending_count: int
    running_count: int
    failed_count: int
    due_count: int
    oldest_due_at: datetime | None


@runtime_checkable
class SharedGuideStore(Protocol):
    """Transactional publication state and public-read operations."""

    def create_publish_intent(
        self,
        draft: SharePublishDraft,
        *,
        now: datetime,
    ) -> ShareIndexIntent: ...

    def stage_update(
        self,
        share_id: str,
        author_user_id: str,
        draft: SharePublishDraft,
        *,
        now: datetime,
        allow_active_upsert_supersede: bool = False,
        expected_index_version: int | None = None,
        expected_content_hash: str | None = None,
        expected_published_at: datetime | None = None,
        expected_indexed_at: datetime | None = None,
    ) -> ShareIndexIntent: ...

    def stage_unpublish(
        self,
        share_id: str,
        author_user_id: str,
        *,
        now: datetime,
    ) -> ShareIndexIntent: ...

    def claim_index_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: float,
    ) -> ShareIndexJob | None: ...

    def claim_next_index_job(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: float,
        max_attempts: int,
    ) -> ShareIndexJob | None: ...

    def count_index_backlog(self, *, now: datetime) -> ShareIndexBacklog: ...

    def complete_index_operation(
        self,
        job_id: str,
        share_id: str,
        index_version: int,
        operation: IndexOperation,
        *,
        worker_id: str,
        now: datetime,
    ) -> bool: ...

    def record_index_failure(
        self,
        job_id: str,
        share_id: str,
        index_version: int,
        operation: IndexOperation,
        error: BaseException | str,
        *,
        worker_id: str,
        next_retry_at: datetime | None,
        terminal: bool,
        now: datetime,
    ) -> bool: ...

    def record_index_job_failure(
        self,
        job_id: str,
        share_id: str,
        index_version: int,
        operation: IndexOperation,
        error: BaseException | str,
        *,
        worker_id: str,
        next_retry_at: datetime | None,
        terminal: bool,
        now: datetime,
    ) -> bool: ...

    def supersede_index_job(
        self,
        job_id: str,
        *,
        worker_id: str | None = None,
        now: datetime,
    ) -> bool: ...

    def put_like(
        self,
        share_id: str,
        user_id: str,
        *,
        now: datetime,
    ) -> LikeMutation: ...

    def delete_like(self, share_id: str, user_id: str) -> LikeMutation: ...

    def get_owned(self, share_id: str, author_user_id: str) -> SharedGuideRecord: ...

    def get_index_record(self, share_id: str) -> SharedGuideRecord | None: ...

    def requeue_current_upsert(
        self,
        share_id: str,
        index_version: int,
        content_hash: str,
        *,
        now: datetime,
    ) -> bool: ...

    def get_for_author_session(
        self,
        author_user_id: str,
        source_session_id: str,
    ) -> SharedGuideRecord | None: ...

    def get_public(
        self,
        share_id: str,
        viewer_user_id: str | None = None,
    ) -> SharedGuidePublicDetail: ...

    def list_public(
        self,
        query: SharedGuideListQuery,
        viewer_user_id: str | None = None,
    ) -> SharedGuidePage: ...

    def list_owned(
        self,
        author_user_id: str,
        query: SharedGuideListQuery,
    ) -> OwnedSharedGuidePage: ...

    def bulk_get_ready(
        self,
        identities: Sequence[IndexedIdentity],
        exclude_session_id: str | None = None,
    ) -> list[SharedGuideRecord]: ...
