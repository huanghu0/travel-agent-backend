"""SQLAlchemy implementation of transactional shared-guide persistence."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from pydantic import RootModel
from sqlalchemy import (
    Connection,
    and_,
    case,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from app.persistence.mysql_base import MySQLStoreBase, as_utc, mysql_utc
from app.persistence.sqlalchemy_models import (
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
    IndexJobStatus,
    IndexOperation,
    LikeMutation,
    OwnedSharedGuideListItem,
    OwnedSharedGuidePage,
    PublicationStatus,
    ShareIndexIntent,
    ShareIndexJob,
    ShareIndexStatus,
    SharePublishDraft,
    SharedGuideListItem,
    SharedGuideListQuery,
    SharedGuidePage,
    SharedGuidePublicDetail,
    SharedGuideRecord,
    SharedGuideSnapshot,
)
from app.sharing.store import ShareIndexBacklog, SharedGuideStore


class _PreferencesJson(RootModel[list[str]]):
    pass


class _CompareAndSetFailed(RuntimeError):
    """Roll back a transaction when a post-lock CAS unexpectedly loses."""


class MySQLSharedGuideStore(MySQLStoreBase, SharedGuideStore):
    """Store shared snapshots and index jobs with compare-and-set transitions."""

    INDEX_RECONCILIATION_ERROR = "IndexReconciliationRequired"
    share_table = SharedGuideRow.__table__
    job_table = ShareIndexJobRow.__table__
    like_table = SharedGuideLikeRow.__table__
    user_table = UserRow.__table__

    @staticmethod
    def _utc(value: datetime, name: str = "timestamp") -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{name} must be timezone-aware UTC")
        return value.astimezone(timezone.utc)

    @classmethod
    def _record_from_row(cls, row: Mapping[str, Any]) -> SharedGuideRecord:
        return SharedGuideRecord(
            share_id=row["share_id"],
            author_user_id=row["author_user_id"],
            source_session_id=row["source_session_id"],
            source_version_id=row["source_version_id"],
            source_version_number=row["source_version_number"],
            title=row["title"],
            city=row["city"],
            city_normalized=row["city_normalized"],
            travel_days=row["travel_days"],
            transportation=row["transportation"],
            accommodation=row["accommodation"],
            preferences=_PreferencesJson.model_validate_json(
                row["preferences_json"]
            ).root,
            snapshot=SharedGuideSnapshot.model_validate_json(row["snapshot_json"]),
            retrieval_text=row["retrieval_text"],
            content_hash=row["content_hash"],
            quality_level=row["quality_level"],
            quality_score=row["quality_score"],
            publication_status=row["publication_status"],
            index_status=row["index_status"],
            embedding_model=row["embedding_model"],
            embedding_dimension=row["embedding_dimension"],
            retrieval_template_version=row["retrieval_template_version"],
            index_version=row["index_version"],
            like_count=row["like_count"],
            last_index_error=row["last_index_error"],
            indexed_at=as_utc(row["indexed_at"]),
            published_at=as_utc(row["published_at"]),
            created_at=as_utc(row["created_at"]),
            updated_at=as_utc(row["updated_at"]),
        )

    @classmethod
    def _job_from_row(cls, row: Mapping[str, Any]) -> ShareIndexJob:
        return ShareIndexJob(
            job_id=row["job_id"],
            share_id=row["share_id"],
            operation=row["operation"],
            index_version=row["index_version"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            next_retry_at=as_utc(row["next_retry_at"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=as_utc(row["lease_expires_at"]),
            last_error=row["last_error"],
            created_at=as_utc(row["created_at"]),
            updated_at=as_utc(row["updated_at"]),
        )

    @staticmethod
    def _cover_image(record: SharedGuideRecord) -> str | None:
        for day in record.snapshot.trip_plan.days:
            for attraction in day.attractions:
                if attraction.image_url:
                    return attraction.image_url
                if attraction.photos:
                    for photo in attraction.photos:
                        if photo:
                            return photo
        return None

    @classmethod
    def _list_item_from_row(cls, row: Mapping[str, Any]) -> SharedGuideListItem:
        record = cls._record_from_row(row)
        if record.published_at is None:
            raise ValueError("listed shared guides require published_at")
        return SharedGuideListItem(
            share_id=record.share_id,
            title=record.title,
            author_username=row["author_username"],
            city=record.city,
            travel_days=record.travel_days,
            transportation=record.transportation,
            accommodation=record.accommodation,
            preferences=record.preferences,
            quality_level=record.quality_level,
            quality_score=record.quality_score,
            like_count=record.like_count,
            published_at=record.published_at,
            cover_image_url=cls._cover_image(record),
            liked_by_me=bool(row["liked_by_me"]),
        )

    @classmethod
    def _detail_from_row(cls, row: Mapping[str, Any]) -> SharedGuidePublicDetail:
        item = cls._list_item_from_row(row)
        record = cls._record_from_row(row)
        return SharedGuidePublicDetail(**item.model_dump(), snapshot=record.snapshot)

    @classmethod
    def _owned_item_from_row(cls, row: Mapping[str, Any]) -> OwnedSharedGuideListItem:
        item = cls._list_item_from_row(row)
        record = cls._record_from_row(row)
        return OwnedSharedGuideListItem(
            **item.model_dump(),
            publication_status=record.publication_status,
            index_status=record.index_status,
            last_index_error=record.last_index_error,
        )

    @staticmethod
    def _draft_values(draft: SharePublishDraft) -> dict[str, Any]:
        return {
            "author_user_id": draft.author_user_id,
            "source_session_id": draft.source_session_id,
            "source_version_id": draft.source_version_id,
            "source_version_number": draft.source_version_number,
            "title": draft.title,
            "city": draft.city,
            "city_normalized": draft.city_normalized,
            "travel_days": draft.travel_days,
            "transportation": draft.transportation,
            "accommodation": draft.accommodation,
            "preferences_json": _PreferencesJson(draft.preferences).model_dump_json(),
            "snapshot_json": draft.snapshot.model_dump_json(),
            "retrieval_text": draft.retrieval_text,
            "content_hash": draft.content_hash,
            "quality_level": draft.quality_level,
            "quality_score": draft.quality_score,
            "embedding_model": draft.embedding_model,
            "embedding_dimension": draft.embedding_dimension,
            "retrieval_template_version": draft.retrieval_template_version,
        }

    @classmethod
    def _select_share_for_update(
        cls,
        connection: Connection,
        share_id: str,
        author_user_id: str | None = None,
        *,
        skip_locked: bool = False,
    ) -> Mapping[str, Any] | None:
        statement = select(cls.share_table).where(cls.share_table.c.share_id == share_id)
        if author_user_id is not None:
            statement = statement.where(
                cls.share_table.c.author_user_id == author_user_id
            )
        if skip_locked:
            statement = statement.with_for_update(skip_locked=True)
        else:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    @classmethod
    def _select_author_session_for_update(
        cls,
        connection: Connection,
        author_user_id: str,
        source_session_id: str,
    ) -> Mapping[str, Any] | None:
        return connection.execute(
            select(cls.share_table)
            .where(
                cls.share_table.c.author_user_id == author_user_id,
                cls.share_table.c.source_session_id == source_session_id,
            )
            .with_for_update()
        ).mappings().one_or_none()

    @classmethod
    def _insert_job(
        cls,
        connection: Connection,
        *,
        share_id: str,
        operation: IndexOperation,
        index_version: int,
        now: datetime,
    ) -> ShareIndexJob:
        job = ShareIndexJob(
            job_id=str(uuid4()),
            share_id=share_id,
            operation=operation,
            index_version=index_version,
            status=IndexJobStatus.PENDING,
            attempt_count=0,
            created_at=now,
            updated_at=now,
        )
        connection.execute(
            cls.job_table.insert().values(
                job_id=job.job_id,
                share_id=job.share_id,
                operation=job.operation.value,
                index_version=job.index_version,
                status=job.status.value,
                attempt_count=0,
                next_retry_at=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
                created_at=mysql_utc(now),
                updated_at=mysql_utc(now),
            )
        )
        return job

    @classmethod
    def _find_version_job(
        cls,
        connection: Connection,
        *,
        share_id: str,
        operation: IndexOperation,
        index_version: int,
    ) -> ShareIndexJob | None:
        row = connection.execute(
            select(cls.job_table).where(
                cls.job_table.c.share_id == share_id,
                cls.job_table.c.operation == operation.value,
                cls.job_table.c.index_version == index_version,
            )
        ).mappings().one_or_none()
        return cls._job_from_row(row) if row is not None else None

    @classmethod
    def _job_snapshot_predicate(cls, row: Mapping[str, Any]):
        predicates = [
            cls.job_table.c.job_id == row["job_id"],
            cls.job_table.c.share_id == row["share_id"],
            cls.job_table.c.operation == row["operation"],
            cls.job_table.c.index_version == row["index_version"],
            cls.job_table.c.status == row["status"],
        ]
        if row["lease_owner"] is None:
            predicates.append(cls.job_table.c.lease_owner.is_(None))
        else:
            predicates.append(cls.job_table.c.lease_owner == row["lease_owner"])
        if row["lease_expires_at"] is None:
            predicates.append(cls.job_table.c.lease_expires_at.is_(None))
        else:
            predicates.append(
                cls.job_table.c.lease_expires_at
                == mysql_utc(as_utc(row["lease_expires_at"]))
            )
        return and_(*predicates)

    @classmethod
    def _is_active_upsert(
        cls,
        row: Mapping[str, Any],
        share_id: str,
        now: datetime,
    ) -> bool:
        lease_expires_at = as_utc(row["lease_expires_at"])
        return (
            row["share_id"] == share_id
            and row["operation"] == IndexOperation.UPSERT.value
            and row["status"] == IndexJobStatus.RUNNING.value
            and lease_expires_at is not None
            and lease_expires_at > now
        )

    @classmethod
    def _supersede_old_upserts(
        cls,
        connection: Connection,
        share_id: str,
        *,
        now: datetime,
        reject_active: bool,
    ) -> None:
        rows = connection.execute(
            select(cls.job_table)
            .where(
                cls.job_table.c.share_id == share_id,
                cls.job_table.c.operation == IndexOperation.UPSERT.value,
                cls.job_table.c.status.in_(
                    (IndexJobStatus.PENDING.value, IndexJobStatus.RUNNING.value)
                ),
            )
            .with_for_update()
        ).mappings().all()
        active_ids = [
            row["job_id"]
            for row in rows
            if cls._is_active_upsert(row, share_id, now)
        ]
        if reject_active and active_ids:
            raise SharedGuideConflictError("分享索引任务仍由有效租约处理")

        superseded_rows = [
            row
            for row in rows
            if row["status"] == IndexJobStatus.PENDING.value
            or (
                row["status"] == IndexJobStatus.RUNNING.value
                and (
                    as_utc(row["lease_expires_at"]) is None
                    or as_utc(row["lease_expires_at"]) <= now
                )
            )
        ]
        for row in superseded_rows:
            candidate = row
            while True:
                result = connection.execute(
                    update(cls.job_table)
                    .where(cls._job_snapshot_predicate(candidate))
                    .values(
                        status=IndexJobStatus.SUCCEEDED.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        next_retry_at=None,
                        updated_at=mysql_utc(now),
                    )
                )
                if result.rowcount == 1:
                    break

                current = connection.execute(
                    select(cls.job_table)
                    .where(cls.job_table.c.job_id == row["job_id"])
                    .with_for_update()
                ).mappings().one_or_none()
                if current is None:
                    break
                if (
                    current["share_id"] != share_id
                    or current["operation"] != IndexOperation.UPSERT.value
                ):
                    break
                if cls._is_active_upsert(current, share_id, now):
                    if reject_active:
                        raise SharedGuideConflictError(
                            "分享索引任务在 supersede 期间取得了有效租约"
                        )
                    break
                if current["status"] in (
                    IndexJobStatus.PENDING.value,
                    IndexJobStatus.RUNNING.value,
                ):
                    candidate = current
                    continue
                if current["status"] in (
                    IndexJobStatus.SUCCEEDED.value,
                    IndexJobStatus.FAILED.value,
                ):
                    break
                raise SharedGuideConflictError("分享索引任务状态已并发变更")

    @classmethod
    def _stage_new_upsert(
        cls,
        connection: Connection,
        row: Mapping[str, Any],
        draft: SharePublishDraft,
        *,
        now: datetime,
        reject_active: bool = True,
        expected_identity: tuple[int, str, datetime, datetime] | None = None,
    ) -> ShareIndexIntent:
        cls._supersede_old_upserts(
            connection,
            row["share_id"],
            now=now,
            reject_active=reject_active,
        )
        index_version = row["index_version"] + 1
        values = cls._draft_values(draft)
        values.update(
            publication_status=PublicationStatus.PUBLISHING.value,
            index_status=ShareIndexStatus.PENDING.value,
            index_version=index_version,
            last_index_error=None,
            indexed_at=None,
            published_at=mysql_utc(now),
            updated_at=mysql_utc(now),
        )
        predicates = [
            cls.share_table.c.share_id == row["share_id"],
            cls.share_table.c.index_version == row["index_version"],
            cls.share_table.c.publication_status == row["publication_status"],
            cls.share_table.c.index_status == row["index_status"],
        ]
        if expected_identity is not None:
            (
                expected_index_version,
                expected_content_hash,
                expected_published_at,
                expected_indexed_at,
            ) = expected_identity
            predicates.extend(
                (
                    cls.share_table.c.index_version == expected_index_version,
                    cls.share_table.c.content_hash == expected_content_hash,
                    cls.share_table.c.publication_status
                    == PublicationStatus.PUBLIC.value,
                    cls.share_table.c.index_status == ShareIndexStatus.READY.value,
                    cls.share_table.c.published_at
                    == mysql_utc(expected_published_at),
                    cls.share_table.c.indexed_at == mysql_utc(expected_indexed_at),
                )
            )
        result = connection.execute(
            update(cls.share_table)
            .where(*predicates)
            .values(**values)
        )
        if result.rowcount != 1:
            raise SharedGuideConflictError("分享状态已并发变更")
        updated_row = dict(row)
        updated_row.update(values)
        job = cls._insert_job(
            connection,
            share_id=row["share_id"],
            operation=IndexOperation.UPSERT,
            index_version=index_version,
            now=now,
        )
        return ShareIndexIntent(
            record=cls._record_from_row(updated_row),
            job=job,
            created=False,
            operation_required=True,
        )

    def create_publish_intent(
        self,
        draft: SharePublishDraft,
        *,
        now: datetime,
    ) -> ShareIndexIntent:
        now = self._utc(now, "now")
        try:
            with self.engine.begin() as connection:
                existing = self._select_author_session_for_update(
                    connection,
                    draft.author_user_id,
                    draft.source_session_id,
                )
                if existing is not None:
                    record = self._record_from_row(existing)
                    if record.publication_status is PublicationStatus.UNPUBLISHED:
                        return self._stage_new_upsert(connection, existing, draft, now=now)
                    if record.content_hash != draft.content_hash:
                        raise SharedGuideConflictError(
                            "当前会话已有活动分享；内容变更必须使用显式更新"
                        )
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
                    job = self._find_version_job(
                        connection,
                        share_id=record.share_id,
                        operation=IndexOperation.UPSERT,
                        index_version=record.index_version,
                    )
                    return ShareIndexIntent(
                        record=record,
                        job=job,
                        created=False,
                        operation_required=(
                            job is not None
                            and job.status
                            in (IndexJobStatus.PENDING, IndexJobStatus.RUNNING)
                        ),
                    )

                share_id = str(uuid4())
                values = self._draft_values(draft)
                values.update(
                    share_id=share_id,
                    publication_status=PublicationStatus.PUBLISHING.value,
                    index_status=ShareIndexStatus.PENDING.value,
                    index_version=1,
                    like_count=0,
                    last_index_error=None,
                    indexed_at=None,
                    published_at=mysql_utc(now),
                    created_at=mysql_utc(now),
                    updated_at=mysql_utc(now),
                )
                connection.execute(self.share_table.insert().values(**values))
                job = self._insert_job(
                    connection,
                    share_id=share_id,
                    operation=IndexOperation.UPSERT,
                    index_version=1,
                    now=now,
                )
                return ShareIndexIntent(
                    record=self._record_from_row(values),
                    job=job,
                    created=True,
                    operation_required=True,
                )
        except IntegrityError as exc:
            existing = self.get_for_author_session(
                draft.author_user_id,
                draft.source_session_id,
            )
            if existing is None or existing.content_hash != draft.content_hash:
                raise SharedGuideConflictError(
                    "当前会话已有不同内容的活动分享"
                ) from exc
            with self.engine.connect() as connection:
                job = self._find_version_job(
                    connection,
                    share_id=existing.share_id,
                    operation=IndexOperation.UPSERT,
                    index_version=existing.index_version,
                )
            return ShareIndexIntent(
                record=existing,
                job=job,
                created=False,
                operation_required=(
                    job is not None
                    and job.status in (IndexJobStatus.PENDING, IndexJobStatus.RUNNING)
                ),
            )

    @classmethod
    def _validate_expected_share_identity(
        cls,
        row: Mapping[str, Any],
        *,
        expected_index_version: int | None,
        expected_content_hash: str | None,
        expected_published_at: datetime | None,
        expected_indexed_at: datetime | None,
    ) -> tuple[int, str, datetime, datetime] | None:
        values = (
            expected_index_version,
            expected_content_hash,
            expected_published_at,
            expected_indexed_at,
        )
        if all(value is None for value in values):
            return None
        if any(value is None for value in values):
            raise ValueError(
                "expected share identity requires version, hash, and both timestamps"
            )
        normalized_published_at = cls._utc(
            expected_published_at,
            "expected_published_at",
        )
        normalized_indexed_at = cls._utc(
            expected_indexed_at,
            "expected_indexed_at",
        )
        if (
            row["publication_status"] != PublicationStatus.PUBLIC.value
            or row["index_status"] != ShareIndexStatus.READY.value
            or row["index_version"] != expected_index_version
            or row["content_hash"] != expected_content_hash
            or as_utc(row["published_at"]) != normalized_published_at
            or as_utc(row["indexed_at"]) != normalized_indexed_at
        ):
            raise SharedGuideConflictError("分享内容已并发变更")
        return (
            expected_index_version,
            expected_content_hash,
            normalized_published_at,
            normalized_indexed_at,
        )

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
    ) -> ShareIndexIntent:
        now = self._utc(now, "now")
        if draft.author_user_id != author_user_id:
            raise SharedGuideConflictError("更新草稿的作者与分享作者不一致")
        with self.engine.begin() as connection:
            row = self._select_share_for_update(connection, share_id, author_user_id)
            if row is None:
                raise SharedGuideNotFoundError(share_id)
            expected_identity = self._validate_expected_share_identity(
                row,
                expected_index_version=expected_index_version,
                expected_content_hash=expected_content_hash,
                expected_published_at=expected_published_at,
                expected_indexed_at=expected_indexed_at,
            )
            if row["source_session_id"] != draft.source_session_id:
                raise SharedGuideConflictError("更新草稿必须来自同一旅行会话")
            if row["publication_status"] == PublicationStatus.UNPUBLISHED.value:
                raise SharedGuideConflictError("已取消公开的分享必须通过会话重新发布")
            return self._stage_new_upsert(
                connection,
                row,
                draft,
                now=now,
                reject_active=not allow_active_upsert_supersede,
                expected_identity=expected_identity,
            )

    def stage_unpublish(
        self,
        share_id: str,
        author_user_id: str,
        *,
        now: datetime,
    ) -> ShareIndexIntent:
        now = self._utc(now, "now")
        with self.engine.begin() as connection:
            row = self._select_share_for_update(connection, share_id, author_user_id)
            if row is None:
                raise SharedGuideNotFoundError(share_id)
            record = self._record_from_row(row)
            if record.publication_status is PublicationStatus.UNPUBLISHED:
                return ShareIndexIntent(
                    record=record,
                    job=None,
                    created=False,
                    operation_required=False,
                )

            self._supersede_old_upserts(
                connection,
                share_id,
                now=now,
                reject_active=False,
            )
            values = {
                "publication_status": PublicationStatus.UNPUBLISHED.value,
                "index_status": ShareIndexStatus.DELETE_PENDING.value,
                "last_index_error": None,
                "updated_at": mysql_utc(now),
            }
            result = connection.execute(
                update(self.share_table)
                .where(
                    self.share_table.c.share_id == share_id,
                    self.share_table.c.index_version == record.index_version,
                    self.share_table.c.publication_status
                    == record.publication_status.value,
                    self.share_table.c.index_status == record.index_status.value,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise SharedGuideConflictError("分享状态已并发变更")
            updated_row = dict(row)
            updated_row.update(values)
            job = self._insert_job(
                connection,
                share_id=share_id,
                operation=IndexOperation.DELETE,
                index_version=record.index_version,
                now=now,
            )
            return ShareIndexIntent(
                record=self._record_from_row(updated_row),
                job=job,
                created=False,
                operation_required=True,
            )

    def claim_index_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: float,
    ) -> ShareIndexJob | None:
        now = self._utc(now, "now")
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self.engine.begin() as connection:
            row = connection.execute(
                select(self.job_table)
                .where(self.job_table.c.job_id == job_id)
                .with_for_update()
            ).mappings().one_or_none()
            if row is None:
                return None
            job = self._job_from_row(row)
            due_pending = (
                job.status is IndexJobStatus.PENDING
                and (job.next_retry_at is None or job.next_retry_at <= now)
            )
            expired_running = (
                job.status is IndexJobStatus.RUNNING
                and (job.lease_expires_at is None or job.lease_expires_at <= now)
            )
            if not (due_pending or expired_running):
                return None

            lease_expires_at = now + timedelta(seconds=lease_seconds)
            result = connection.execute(
                update(self.job_table)
                .where(
                    self.job_table.c.job_id == job_id,
                    or_(
                        and_(
                            self.job_table.c.status == IndexJobStatus.PENDING.value,
                            or_(
                                self.job_table.c.next_retry_at.is_(None),
                                self.job_table.c.next_retry_at <= mysql_utc(now),
                            ),
                        ),
                        and_(
                            self.job_table.c.status == IndexJobStatus.RUNNING.value,
                            or_(
                                self.job_table.c.lease_expires_at.is_(None),
                                self.job_table.c.lease_expires_at <= mysql_utc(now),
                            ),
                        ),
                    ),
                )
                .values(
                    status=IndexJobStatus.RUNNING.value,
                    attempt_count=job.attempt_count + 1,
                    next_retry_at=None,
                    lease_owner=worker_id,
                    lease_expires_at=mysql_utc(lease_expires_at),
                    updated_at=mysql_utc(now),
                )
            )
            if result.rowcount != 1:
                return None
            payload = dict(row)
            payload.update(
                status=IndexJobStatus.RUNNING.value,
                attempt_count=job.attempt_count + 1,
                next_retry_at=None,
                lease_owner=worker_id,
                lease_expires_at=mysql_utc(lease_expires_at),
                updated_at=mysql_utc(now),
            )
            return self._job_from_row(payload)

    @classmethod
    def _due_job_predicate(cls, now: datetime):
        return or_(
            and_(
                cls.job_table.c.status == IndexJobStatus.PENDING.value,
                or_(
                    cls.job_table.c.next_retry_at.is_(None),
                    cls.job_table.c.next_retry_at <= mysql_utc(now),
                ),
            ),
            and_(
                cls.job_table.c.status == IndexJobStatus.RUNNING.value,
                or_(
                    cls.job_table.c.lease_expires_at.is_(None),
                    cls.job_table.c.lease_expires_at <= mysql_utc(now),
                ),
            ),
        )

    @classmethod
    def _claim_candidate_predicate(cls, now: datetime, max_attempts: int):
        return or_(
            and_(
                cls.job_table.c.status == IndexJobStatus.PENDING.value,
                cls.job_table.c.attempt_count < max_attempts,
                or_(
                    cls.job_table.c.next_retry_at.is_(None),
                    cls.job_table.c.next_retry_at <= mysql_utc(now),
                ),
            ),
            and_(
                cls.job_table.c.status == IndexJobStatus.RUNNING.value,
                or_(
                    cls.job_table.c.lease_expires_at.is_(None),
                    cls.job_table.c.lease_expires_at <= mysql_utc(now),
                ),
            ),
        )

    @classmethod
    def _oldest_claim_candidate(
        cls,
        connection: Connection,
        now: datetime,
        max_attempts: int,
        *,
        excluded_job_ids: Sequence[str] = (),
    ):
        statement = select(cls.job_table).where(
            cls._claim_candidate_predicate(now, max_attempts)
        )
        if excluded_job_ids:
            statement = statement.where(
                cls.job_table.c.job_id.not_in(tuple(excluded_job_ids))
            )
        return connection.execute(
            statement.order_by(
                cls.job_table.c.created_at,
                cls.job_table.c.job_id,
            ).limit(1)
        ).mappings().one_or_none()

    @classmethod
    def _terminalize_expired_claim(
        cls,
        connection: Connection,
        row: Mapping[str, Any],
        *,
        now: datetime,
    ) -> bool:
        if row["operation"] == IndexOperation.UPSERT.value:
            expected_publication = PublicationStatus.PUBLISHING.value
            expected_indexes = (
                ShareIndexStatus.PENDING.value,
                ShareIndexStatus.FAILED.value,
            )
        else:
            expected_publication = PublicationStatus.UNPUBLISHED.value
            expected_indexes = (
                ShareIndexStatus.DELETE_PENDING.value,
                ShareIndexStatus.FAILED.value,
            )
        connection.execute(
            update(cls.share_table)
            .where(
                cls.share_table.c.share_id == row["share_id"],
                cls.share_table.c.index_version == row["index_version"],
                cls.share_table.c.publication_status == expected_publication,
                cls.share_table.c.index_status.in_(expected_indexes),
            )
            .values(
                index_status=ShareIndexStatus.FAILED.value,
                last_index_error="IndexLeaseExpired",
                updated_at=mysql_utc(now),
            )
        )
        result = connection.execute(
            update(cls.job_table)
            .where(
                cls._job_snapshot_predicate(row),
                cls.job_table.c.status == IndexJobStatus.RUNNING.value,
                cls.job_table.c.attempt_count == row["attempt_count"],
                or_(
                    cls.job_table.c.lease_expires_at.is_(None),
                    cls.job_table.c.lease_expires_at <= mysql_utc(now),
                ),
            )
            .values(
                status=IndexJobStatus.FAILED.value,
                next_retry_at=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error="IndexLeaseExpired",
                updated_at=mysql_utc(now),
            )
        )
        return result.rowcount == 1

    def claim_next_index_job(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_seconds: float,
        max_attempts: int,
    ) -> ShareIndexJob | None:
        now = self._utc(now, "now")
        worker_id = worker_id.strip()
        if not worker_id or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1 to 128 characters")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        if self.engine.dialect.name == "sqlite":
            while True:
                try:
                    with self.engine.begin() as connection:
                        candidate = self._oldest_claim_candidate(
                            connection,
                            now,
                            max_attempts,
                        )
                        if candidate is None:
                            return None
                        if (
                            candidate["status"] == IndexJobStatus.RUNNING.value
                            and candidate["attempt_count"] >= max_attempts
                        ):
                            if not self._terminalize_expired_claim(
                                connection,
                                candidate,
                                now=now,
                            ):
                                raise _CompareAndSetFailed
                            continue
                        lease_expires_at = now + timedelta(seconds=lease_seconds)
                        result = connection.execute(
                            update(self.job_table)
                            .where(
                                self._job_snapshot_predicate(candidate),
                                self._due_job_predicate(now),
                                self.job_table.c.attempt_count < max_attempts,
                            )
                            .values(
                                status=IndexJobStatus.RUNNING.value,
                                attempt_count=self.job_table.c.attempt_count + 1,
                                next_retry_at=None,
                                lease_owner=worker_id,
                                lease_expires_at=mysql_utc(lease_expires_at),
                                updated_at=mysql_utc(now),
                            )
                        )
                        if result.rowcount != 1:
                            raise _CompareAndSetFailed
                        payload = dict(candidate)
                        payload.update(
                            status=IndexJobStatus.RUNNING.value,
                            attempt_count=candidate["attempt_count"] + 1,
                            next_retry_at=None,
                            lease_owner=worker_id,
                            lease_expires_at=mysql_utc(lease_expires_at),
                            updated_at=mysql_utc(now),
                        )
                        return self._job_from_row(payload)
                except _CompareAndSetFailed:
                    continue

        skipped_job_ids: set[str] = set()
        while True:
            with self.engine.begin() as connection:
                candidate = self._oldest_claim_candidate(
                    connection,
                    now,
                    max_attempts,
                    excluded_job_ids=tuple(skipped_job_ids),
                )
                if candidate is None:
                    return None
                share_row = self._select_share_for_update(
                    connection,
                    candidate["share_id"],
                    skip_locked=True,
                )
                if share_row is None:
                    skipped_job_ids.add(candidate["job_id"])
                    continue
                row = connection.execute(
                    select(self.job_table)
                    .where(
                        # The transactional global read chooses the exact job.
                        # Recheck it after taking the share lock; selecting a
                        # different job for this share would violate ordering.
                        self.job_table.c.job_id == candidate["job_id"],
                        self._claim_candidate_predicate(now, max_attempts),
                    )
                    .with_for_update(skip_locked=True)
                ).mappings().one_or_none()
                if row is None:
                    skipped_job_ids.add(candidate["job_id"])
                    continue
                if (
                    row["status"] == IndexJobStatus.RUNNING.value
                    and row["attempt_count"] >= max_attempts
                ):
                    if not self._terminalize_expired_claim(connection, row, now=now):
                        skipped_job_ids.add(candidate["job_id"])
                    continue
                if row["attempt_count"] >= max_attempts:
                    skipped_job_ids.add(candidate["job_id"])
                    continue
                lease_expires_at = now + timedelta(seconds=lease_seconds)
                result = connection.execute(
                    update(self.job_table)
                    .where(
                        self._job_snapshot_predicate(row),
                        self._due_job_predicate(now),
                        self.job_table.c.attempt_count < max_attempts,
                    )
                    .values(
                        status=IndexJobStatus.RUNNING.value,
                        attempt_count=self.job_table.c.attempt_count + 1,
                        next_retry_at=None,
                        lease_owner=worker_id,
                        lease_expires_at=mysql_utc(lease_expires_at),
                        updated_at=mysql_utc(now),
                    )
                )
                if result.rowcount != 1:
                    continue
                payload = dict(row)
                payload.update(
                    status=IndexJobStatus.RUNNING.value,
                    attempt_count=row["attempt_count"] + 1,
                    next_retry_at=None,
                    lease_owner=worker_id,
                    lease_expires_at=mysql_utc(lease_expires_at),
                    updated_at=mysql_utc(now),
                )
                return self._job_from_row(payload)

    def count_index_backlog(self, *, now: datetime) -> ShareIndexBacklog:
        now = self._utc(now, "now")
        due = self._due_job_predicate(now)
        due_at = case(
            (
                and_(
                    self.job_table.c.status == IndexJobStatus.PENDING.value,
                    due,
                ),
                func.coalesce(
                    self.job_table.c.next_retry_at,
                    self.job_table.c.created_at,
                ),
            ),
            (
                and_(
                    self.job_table.c.status == IndexJobStatus.RUNNING.value,
                    due,
                ),
                func.coalesce(
                    self.job_table.c.lease_expires_at,
                    self.job_table.c.created_at,
                ),
            ),
            else_=None,
        )
        statement = select(
            func.sum(
                case(
                    (self.job_table.c.status == IndexJobStatus.PENDING.value, 1),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (self.job_table.c.status == IndexJobStatus.RUNNING.value, 1),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (self.job_table.c.status == IndexJobStatus.FAILED.value, 1),
                    else_=0,
                )
            ),
            func.sum(case((due, 1), else_=0)),
            func.min(due_at),
        )
        with self.engine.connect() as connection:
            pending, running, failed, due_count, oldest_due_at = connection.execute(
                statement
            ).one()
        return ShareIndexBacklog(
            pending_count=int(pending or 0),
            running_count=int(running or 0),
            failed_count=int(failed or 0),
            due_count=int(due_count or 0),
            oldest_due_at=as_utc(oldest_due_at),
        )

    def requeue_current_upsert(
        self,
        share_id: str,
        index_version: int,
        content_hash: str,
        *,
        now: datetime,
    ) -> bool:
        """Hide a ready current version and schedule a bounded repair attempt.

        A stale writer can overwrite the single Qdrant point after the current
        version has already become PUBLIC/READY.  This transition preserves the
        share-first/job-second lock order and only demotes the exact persisted
        current version whose successful UPSERT job is being repaired.
        """

        now = self._utc(now, "now")
        try:
            with self.engine.begin() as connection:
                share_row = self._select_share_for_update(connection, share_id)
                if (
                    share_row is None
                    or share_row["index_version"] != index_version
                    or share_row["content_hash"] != content_hash
                    or share_row["publication_status"]
                    != PublicationStatus.PUBLIC.value
                    or share_row["index_status"] != ShareIndexStatus.READY.value
                ):
                    return False

                job_row = connection.execute(
                    select(self.job_table)
                    .where(
                        self.job_table.c.share_id == share_id,
                        self.job_table.c.operation == IndexOperation.UPSERT.value,
                        self.job_table.c.index_version == index_version,
                        self.job_table.c.status == IndexJobStatus.SUCCEEDED.value,
                    )
                    .with_for_update()
                ).mappings().one_or_none()
                if job_row is None:
                    return False

                share_result = connection.execute(
                    update(self.share_table)
                    .where(
                        self.share_table.c.share_id == share_id,
                        self.share_table.c.index_version == index_version,
                        self.share_table.c.content_hash == content_hash,
                        self.share_table.c.publication_status
                        == PublicationStatus.PUBLIC.value,
                        self.share_table.c.index_status == ShareIndexStatus.READY.value,
                    )
                    .values(
                        publication_status=PublicationStatus.PUBLISHING.value,
                        index_status=ShareIndexStatus.PENDING.value,
                        indexed_at=None,
                        last_index_error=self.INDEX_RECONCILIATION_ERROR,
                        updated_at=mysql_utc(now),
                    )
                )
                if share_result.rowcount != 1:
                    raise _CompareAndSetFailed

                job_result = connection.execute(
                    update(self.job_table)
                    .where(
                        self.job_table.c.job_id == job_row["job_id"],
                        self.job_table.c.share_id == share_id,
                        self.job_table.c.operation == IndexOperation.UPSERT.value,
                        self.job_table.c.index_version == index_version,
                        self.job_table.c.status == IndexJobStatus.SUCCEEDED.value,
                    )
                    .values(
                        status=IndexJobStatus.PENDING.value,
                        attempt_count=0,
                        next_retry_at=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error=self.INDEX_RECONCILIATION_ERROR,
                        updated_at=mysql_utc(now),
                    )
                )
                if job_result.rowcount != 1:
                    raise _CompareAndSetFailed
        except _CompareAndSetFailed:
            return False
        return True

    @classmethod
    def _lease_matches(
        cls,
        row: Mapping[str, Any],
        *,
        worker_id: str,
        now: datetime,
    ) -> bool:
        expires_at = as_utc(row["lease_expires_at"])
        return (
            row["status"] == IndexJobStatus.RUNNING.value
            and row["lease_owner"] == worker_id
            and expires_at is not None
            and expires_at > now
        )

    def complete_index_operation(
        self,
        job_id: str,
        share_id: str,
        index_version: int,
        operation: IndexOperation,
        *,
        worker_id: str,
        now: datetime,
    ) -> bool:
        now = self._utc(now, "now")
        operation = IndexOperation(operation)
        try:
            with self.engine.begin() as connection:
                share_row = self._select_share_for_update(connection, share_id)
                if share_row is None or share_row["index_version"] != index_version:
                    return False
                job_row = connection.execute(
                    select(self.job_table)
                    .where(self.job_table.c.job_id == job_id)
                    .with_for_update()
                ).mappings().one_or_none()
                if (
                    job_row is None
                    or job_row["share_id"] != share_id
                    or job_row["index_version"] != index_version
                    or job_row["operation"] != operation.value
                    or not self._lease_matches(job_row, worker_id=worker_id, now=now)
                ):
                    return False

                if operation is IndexOperation.UPSERT:
                    expected_publication = PublicationStatus.PUBLISHING.value
                    expected_indexes = (
                        ShareIndexStatus.PENDING.value,
                        ShareIndexStatus.FAILED.value,
                    )
                    share_values = {
                        "publication_status": PublicationStatus.PUBLIC.value,
                        "index_status": ShareIndexStatus.READY.value,
                        "indexed_at": mysql_utc(now),
                        "last_index_error": None,
                        "updated_at": mysql_utc(now),
                    }
                else:
                    expected_publication = PublicationStatus.UNPUBLISHED.value
                    expected_indexes = (
                        ShareIndexStatus.DELETE_PENDING.value,
                        ShareIndexStatus.FAILED.value,
                    )
                    share_values = {
                        "publication_status": PublicationStatus.UNPUBLISHED.value,
                        "index_status": ShareIndexStatus.DELETED.value,
                        "last_index_error": None,
                        "updated_at": mysql_utc(now),
                    }
                share_result = connection.execute(
                    update(self.share_table)
                    .where(
                        self.share_table.c.share_id == share_id,
                        self.share_table.c.index_version == index_version,
                        self.share_table.c.publication_status == expected_publication,
                        self.share_table.c.index_status.in_(expected_indexes),
                    )
                    .values(**share_values)
                )
                if share_result.rowcount != 1:
                    raise _CompareAndSetFailed
                job_result = connection.execute(
                    update(self.job_table)
                    .where(
                        self.job_table.c.job_id == job_id,
                        self.job_table.c.share_id == share_id,
                        self.job_table.c.index_version == index_version,
                        self.job_table.c.operation == operation.value,
                        self.job_table.c.status == IndexJobStatus.RUNNING.value,
                        self.job_table.c.lease_owner == worker_id,
                        self.job_table.c.lease_expires_at > mysql_utc(now),
                    )
                    .values(
                        status=IndexJobStatus.SUCCEEDED.value,
                        next_retry_at=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error=None,
                        updated_at=mysql_utc(now),
                    )
                )
                if job_result.rowcount != 1:
                    raise _CompareAndSetFailed
        except _CompareAndSetFailed:
            return False
        return True

    @classmethod
    def _sanitize_error(cls, error: BaseException | str) -> str:
        error_type = type(error).__name__ if isinstance(error, BaseException) else "IndexError"
        return error_type[:1000]

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
    ) -> bool:
        now = self._utc(now, "now")
        if not terminal and next_retry_at is None:
            raise ValueError("retryable failures require next_retry_at")
        if next_retry_at is not None:
            next_retry_at = self._utc(next_retry_at, "next_retry_at")
        operation = IndexOperation(operation)
        sanitized = self._sanitize_error(error)
        try:
            with self.engine.begin() as connection:
                share_row = self._select_share_for_update(connection, share_id)
                if share_row is None or share_row["index_version"] != index_version:
                    return False
                job_row = connection.execute(
                    select(self.job_table)
                    .where(self.job_table.c.job_id == job_id)
                    .with_for_update()
                ).mappings().one_or_none()
                if (
                    job_row is None
                    or job_row["share_id"] != share_id
                    or job_row["index_version"] != index_version
                    or job_row["operation"] != operation.value
                    or not self._lease_matches(job_row, worker_id=worker_id, now=now)
                ):
                    return False

                if operation is IndexOperation.UPSERT:
                    expected_publication = PublicationStatus.PUBLISHING.value
                    expected_indexes = (
                        ShareIndexStatus.PENDING.value,
                        ShareIndexStatus.FAILED.value,
                    )
                    next_index_status = ShareIndexStatus.FAILED.value
                else:
                    expected_publication = PublicationStatus.UNPUBLISHED.value
                    expected_indexes = (
                        ShareIndexStatus.DELETE_PENDING.value,
                        ShareIndexStatus.FAILED.value,
                    )
                    next_index_status = (
                        ShareIndexStatus.FAILED.value
                        if terminal
                        else ShareIndexStatus.DELETE_PENDING.value
                    )
                share_result = connection.execute(
                    update(self.share_table)
                    .where(
                        self.share_table.c.share_id == share_id,
                        self.share_table.c.index_version == index_version,
                        self.share_table.c.publication_status == expected_publication,
                        self.share_table.c.index_status.in_(expected_indexes),
                    )
                    .values(
                        index_status=next_index_status,
                        last_index_error=sanitized,
                        updated_at=mysql_utc(now),
                    )
                )
                if share_result.rowcount != 1:
                    raise _CompareAndSetFailed
                job_result = connection.execute(
                    update(self.job_table)
                    .where(
                        self.job_table.c.job_id == job_id,
                        self.job_table.c.share_id == share_id,
                        self.job_table.c.index_version == index_version,
                        self.job_table.c.operation == operation.value,
                        self.job_table.c.status == IndexJobStatus.RUNNING.value,
                        self.job_table.c.lease_owner == worker_id,
                        self.job_table.c.lease_expires_at > mysql_utc(now),
                    )
                    .values(
                        status=(
                            IndexJobStatus.FAILED.value
                            if terminal
                            else IndexJobStatus.PENDING.value
                        ),
                        next_retry_at=(None if terminal else mysql_utc(next_retry_at)),
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error=sanitized,
                        updated_at=mysql_utc(now),
                    )
                )
                if job_result.rowcount != 1:
                    raise _CompareAndSetFailed
        except _CompareAndSetFailed:
            return False
        return True

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
    ) -> bool:
        """Record a stale-job repair failure without rewriting newer share state."""

        now = self._utc(now, "now")
        if not terminal and next_retry_at is None:
            raise ValueError("retryable failures require next_retry_at")
        if next_retry_at is not None:
            next_retry_at = self._utc(next_retry_at, "next_retry_at")
        operation = IndexOperation(operation)
        sanitized = self._sanitize_error(error)
        try:
            with self.engine.begin() as connection:
                share_row = self._select_share_for_update(connection, share_id)
                if share_row is None:
                    return False
                job_row = connection.execute(
                    select(self.job_table)
                    .where(self.job_table.c.job_id == job_id)
                    .with_for_update()
                ).mappings().one_or_none()
                if (
                    job_row is None
                    or job_row["share_id"] != share_id
                    or job_row["index_version"] != index_version
                    or job_row["operation"] != operation.value
                    or not self._lease_matches(job_row, worker_id=worker_id, now=now)
                ):
                    return False
                result = connection.execute(
                    update(self.job_table)
                    .where(
                        self._job_snapshot_predicate(job_row),
                        self.job_table.c.status == IndexJobStatus.RUNNING.value,
                        self.job_table.c.lease_owner == worker_id,
                        self.job_table.c.lease_expires_at > mysql_utc(now),
                    )
                    .values(
                        status=(
                            IndexJobStatus.FAILED.value
                            if terminal
                            else IndexJobStatus.PENDING.value
                        ),
                        next_retry_at=(None if terminal else mysql_utc(next_retry_at)),
                        lease_owner=None,
                        lease_expires_at=None,
                        last_error=sanitized,
                        updated_at=mysql_utc(now),
                    )
                )
                if result.rowcount != 1:
                    raise _CompareAndSetFailed
        except _CompareAndSetFailed:
            return False
        return True

    @classmethod
    def _job_effect_is_obsolete(
        cls,
        share_row: Mapping[str, Any],
        job_row: Mapping[str, Any],
    ) -> bool:
        if share_row["index_version"] > job_row["index_version"]:
            return True
        if share_row["index_version"] < job_row["index_version"]:
            return False
        if job_row["operation"] == IndexOperation.UPSERT.value:
            return share_row["publication_status"] == PublicationStatus.UNPUBLISHED.value or (
                share_row["publication_status"] == PublicationStatus.PUBLIC.value
                and share_row["index_status"] == ShareIndexStatus.READY.value
            )
        if job_row["operation"] == IndexOperation.DELETE.value:
            return share_row["publication_status"] == PublicationStatus.PUBLIC.value or (
                share_row["publication_status"] == PublicationStatus.UNPUBLISHED.value
                and share_row["index_status"] == ShareIndexStatus.DELETED.value
            )
        return False

    @classmethod
    def _job_obsolete_predicate(
        cls,
        share_row: Mapping[str, Any],
        job_row: Mapping[str, Any],
    ):
        predicate = cls.job_table.c.index_version < share_row["index_version"]
        if share_row["index_version"] == job_row["index_version"] and cls._job_effect_is_obsolete(
            share_row, job_row
        ):
            predicate = or_(
                predicate,
                cls.job_table.c.index_version == share_row["index_version"],
            )
        return predicate

    def supersede_index_job(
        self,
        job_id: str,
        *,
        worker_id: str | None = None,
        now: datetime,
    ) -> bool:
        now = self._utc(now, "now")
        with self.engine.begin() as connection:
            job_share_id = (
                select(self.job_table.c.share_id)
                .where(self.job_table.c.job_id == job_id)
                .scalar_subquery()
            )
            share_row = connection.execute(
                select(self.share_table)
                .where(self.share_table.c.share_id == job_share_id)
                .with_for_update()
            ).mappings().one_or_none()
            if share_row is None:
                return False

            authorization = select(self.job_table).where(
                self.job_table.c.job_id == job_id
            )
            if worker_id is not None:
                authorization = authorization.where(
                    self.job_table.c.status == IndexJobStatus.RUNNING.value,
                    self.job_table.c.lease_owner == worker_id,
                    self.job_table.c.lease_expires_at.is_not(None),
                    self.job_table.c.lease_expires_at > mysql_utc(now),
                )
            row = connection.execute(
                authorization.with_for_update()
            ).mappings().one_or_none()
            if row is None:
                return False
            if row["share_id"] != share_row["share_id"]:
                return False
            if worker_id is None:
                allowed = row["status"] == IndexJobStatus.PENDING.value or (
                    row["status"] == IndexJobStatus.RUNNING.value
                    and (
                        as_utc(row["lease_expires_at"]) is None
                        or as_utc(row["lease_expires_at"]) <= now
                    )
                )
            else:
                allowed = (
                    row["status"] == IndexJobStatus.RUNNING.value
                    and row["lease_owner"] == worker_id
                    and as_utc(row["lease_expires_at"]) is not None
                    and as_utc(row["lease_expires_at"]) > now
                )
            if not allowed:
                return False
            if not self._job_effect_is_obsolete(share_row, row):
                return False
            if worker_id is None:
                current_state = or_(
                    self.job_table.c.status == IndexJobStatus.PENDING.value,
                    and_(
                        self.job_table.c.status == IndexJobStatus.RUNNING.value,
                        or_(
                            self.job_table.c.lease_expires_at.is_(None),
                            self.job_table.c.lease_expires_at <= mysql_utc(now),
                        ),
                    ),
                )
            else:
                current_state = and_(
                    self.job_table.c.status == IndexJobStatus.RUNNING.value,
                    self.job_table.c.lease_owner == worker_id,
                    self.job_table.c.lease_expires_at.is_not(None),
                    self.job_table.c.lease_expires_at > mysql_utc(now),
                )
            result = connection.execute(
                update(self.job_table)
                .where(
                    self._job_snapshot_predicate(row),
                    current_state,
                    self._job_obsolete_predicate(share_row, row),
                )
                .values(
                    status=IndexJobStatus.SUCCEEDED.value,
                    next_retry_at=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=mysql_utc(now),
                )
            )
            return result.rowcount == 1

    @classmethod
    def _require_public_ready_share_for_update(
        cls,
        connection: Connection,
        share_id: str,
    ) -> Mapping[str, Any]:
        row = cls._select_share_for_update(connection, share_id)
        if (
            row is None
            or row["publication_status"] != PublicationStatus.PUBLIC.value
            or row["index_status"] != ShareIndexStatus.READY.value
        ):
            raise SharedGuideNotFoundError(share_id)
        return row

    def put_like(
        self,
        share_id: str,
        user_id: str,
        *,
        now: datetime,
    ) -> LikeMutation:
        now = self._utc(now, "now")
        with self.engine.begin() as connection:
            share = self._require_public_ready_share_for_update(connection, share_id)
            if share["author_user_id"] == user_id:
                raise SharedGuideForbiddenError("作者不能点赞自己的分享")
            values = {
                "like_id": str(uuid4()),
                "share_id": share_id,
                "user_id": user_id,
                "created_at": mysql_utc(now),
            }
            if connection.dialect.name == "sqlite":
                statement = sqlite_insert(self.like_table).values(**values).on_conflict_do_nothing(
                    index_elements=["share_id", "user_id"]
                )
            else:
                statement = mysql.insert(self.like_table).values(**values).prefix_with("IGNORE")
            inserted = connection.execute(statement)
            if inserted.rowcount == 1:
                connection.execute(
                    update(self.share_table)
                    .where(self.share_table.c.share_id == share_id)
                    .values(like_count=self.share_table.c.like_count + 1)
                )
            like_count = connection.execute(
                select(self.share_table.c.like_count).where(
                    self.share_table.c.share_id == share_id
                )
            ).scalar_one()
            return LikeMutation(liked=True, like_count=like_count)

    def delete_like(self, share_id: str, user_id: str) -> LikeMutation:
        with self.engine.begin() as connection:
            share = self._require_public_ready_share_for_update(connection, share_id)
            if share["author_user_id"] == user_id:
                raise SharedGuideForbiddenError("作者不能取消自己分享的点赞")
            deleted = connection.execute(
                delete(self.like_table).where(
                    self.like_table.c.share_id == share_id,
                    self.like_table.c.user_id == user_id,
                )
            )
            if deleted.rowcount == 1:
                connection.execute(
                    update(self.share_table)
                    .where(self.share_table.c.share_id == share_id)
                    .values(
                        like_count=case(
                            (self.share_table.c.like_count > 0, self.share_table.c.like_count - 1),
                            else_=0,
                        )
                    )
                )
            like_count = connection.execute(
                select(self.share_table.c.like_count).where(
                    self.share_table.c.share_id == share_id
                )
            ).scalar_one()
            return LikeMutation(liked=False, like_count=like_count)

    def get_owned(self, share_id: str, author_user_id: str) -> SharedGuideRecord:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.share_table).where(
                    self.share_table.c.share_id == share_id,
                    self.share_table.c.author_user_id == author_user_id,
                )
            ).mappings().one_or_none()
        if row is None:
            raise SharedGuideNotFoundError(share_id)
        return self._record_from_row(row)

    def get_index_record(self, share_id: str) -> SharedGuideRecord | None:
        """Load an exact indexing target without exposing it as a public read."""

        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.share_table).where(self.share_table.c.share_id == share_id)
            ).mappings().one_or_none()
        return self._record_from_row(row) if row is not None else None

    def get_for_author_session(
        self,
        author_user_id: str,
        source_session_id: str,
    ) -> SharedGuideRecord | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.share_table).where(
                    self.share_table.c.author_user_id == author_user_id,
                    self.share_table.c.source_session_id == source_session_id,
                )
            ).mappings().one_or_none()
        return self._record_from_row(row) if row is not None else None

    @classmethod
    def _liked_expression(cls, viewer_user_id: str | None):
        if viewer_user_id is None:
            return literal(False).label("liked_by_me")
        return exists(
            select(cls.like_table.c.like_id).where(
                cls.like_table.c.share_id == cls.share_table.c.share_id,
                cls.like_table.c.user_id == viewer_user_id,
            )
        ).label("liked_by_me")

    @classmethod
    def _public_select(cls, viewer_user_id: str | None):
        return select(
            *cls.share_table.c,
            cls.user_table.c.username.label("author_username"),
            cls._liked_expression(viewer_user_id),
        ).join(
            cls.user_table,
            cls.user_table.c.user_id == cls.share_table.c.author_user_id,
        )

    def get_public(
        self,
        share_id: str,
        viewer_user_id: str | None = None,
    ) -> SharedGuidePublicDetail:
        with self.engine.connect() as connection:
            row = connection.execute(
                self._public_select(viewer_user_id).where(
                    self.share_table.c.share_id == share_id,
                    self.share_table.c.publication_status
                    == PublicationStatus.PUBLIC.value,
                    self.share_table.c.index_status == ShareIndexStatus.READY.value,
                )
            ).mappings().one_or_none()
        if row is None:
            raise SharedGuideNotFoundError(share_id)
        return self._detail_from_row(row)

    @staticmethod
    def _encode_cursor(item: SharedGuideListItem, sort: str) -> str:
        payload: dict[str, Any] = {
            "v": 1,
            "sort": sort,
        }
        if sort == "popular":
            payload["like_count"] = item.like_count
        payload.update(
            published_at=item.published_at.astimezone(timezone.utc).isoformat(),
            share_id=item.share_id,
        )
        compact = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(compact).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, expected_sort: str) -> dict[str, Any]:
        try:
            encoded = cursor.encode("ascii")
            padding = b"=" * ((4 - len(encoded) % 4) % 4)
            raw = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("cursor payload must be an object")
            expected_keys = {"v", "sort", "published_at", "share_id"}
            if expected_sort == "popular":
                expected_keys.add("like_count")
            if set(payload) != expected_keys:
                raise ValueError("cursor fields do not match sort")
            if (
                isinstance(payload["v"], bool)
                or not isinstance(payload["v"], int)
                or payload["v"] != 1
                or payload["sort"] != expected_sort
            ):
                raise ValueError("cursor version or sort mismatch")
            published_at = datetime.fromisoformat(payload["published_at"])
            if (
                published_at.tzinfo is None
                or published_at.utcoffset() != timedelta(0)
            ):
                raise ValueError("cursor timestamp must be UTC")
            share_id = payload["share_id"]
            if not isinstance(share_id, str):
                raise ValueError("cursor share_id must be a string")
            UUID(share_id)
            result: dict[str, Any] = {
                "published_at": published_at.astimezone(timezone.utc),
                "share_id": share_id,
            }
            if expected_sort == "popular":
                like_count = payload["like_count"]
                if isinstance(like_count, bool) or not isinstance(like_count, int) or like_count < 0:
                    raise ValueError("cursor like_count is invalid")
                result["like_count"] = like_count
            return result
        except (
            UnicodeEncodeError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise InvalidShareCursorError("无效的分享列表游标") from exc

    @classmethod
    def _apply_keyset(
        cls,
        statement,
        query: SharedGuideListQuery,
    ):
        cursor = (
            cls._decode_cursor(query.cursor, query.sort)
            if query.cursor is not None
            else None
        )
        published_at = cls.share_table.c.published_at
        share_id = cls.share_table.c.share_id
        like_count = cls.share_table.c.like_count
        if query.sort == "latest":
            if cursor is not None:
                statement = statement.where(
                    or_(
                        published_at < mysql_utc(cursor["published_at"]),
                        and_(
                            published_at == mysql_utc(cursor["published_at"]),
                            share_id < cursor["share_id"],
                        ),
                    )
                )
            return statement.order_by(published_at.desc(), share_id.desc())
        if cursor is not None:
            statement = statement.where(
                or_(
                    like_count < cursor["like_count"],
                    and_(
                        like_count == cursor["like_count"],
                        published_at < mysql_utc(cursor["published_at"]),
                    ),
                    and_(
                        like_count == cursor["like_count"],
                        published_at == mysql_utc(cursor["published_at"]),
                        share_id < cursor["share_id"],
                    ),
                )
            )
        return statement.order_by(
            like_count.desc(),
            published_at.desc(),
            share_id.desc(),
        )

    def list_public(
        self,
        query: SharedGuideListQuery,
        viewer_user_id: str | None = None,
    ) -> SharedGuidePage:
        statement = self._public_select(viewer_user_id).where(
            self.share_table.c.publication_status == PublicationStatus.PUBLIC.value,
            self.share_table.c.index_status == ShareIndexStatus.READY.value,
            self.share_table.c.published_at.is_not(None),
        )
        if query.city_normalized is not None:
            statement = statement.where(
                self.share_table.c.city_normalized == query.city_normalized
            )
        if query.travel_days is not None:
            statement = statement.where(
                self.share_table.c.travel_days == query.travel_days
            )
        if query.transportation is not None:
            statement = statement.where(
                self.share_table.c.transportation == query.transportation
            )
        statement = self._apply_keyset(statement, query).limit(query.limit + 1)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        has_more = len(rows) > query.limit
        items = [self._list_item_from_row(row) for row in rows[: query.limit]]
        next_cursor = (
            self._encode_cursor(items[-1], query.sort)
            if has_more and items
            else None
        )
        return SharedGuidePage(items=items, next_cursor=next_cursor)

    def list_owned(
        self,
        author_user_id: str,
        query: SharedGuideListQuery,
    ) -> OwnedSharedGuidePage:
        statement = self._public_select(None).where(
            self.share_table.c.author_user_id == author_user_id,
            self.share_table.c.published_at.is_not(None),
        )
        if query.city_normalized is not None:
            statement = statement.where(
                self.share_table.c.city_normalized == query.city_normalized
            )
        if query.travel_days is not None:
            statement = statement.where(
                self.share_table.c.travel_days == query.travel_days
            )
        if query.transportation is not None:
            statement = statement.where(
                self.share_table.c.transportation == query.transportation
            )
        statement = self._apply_keyset(statement, query).limit(query.limit + 1)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        has_more = len(rows) > query.limit
        items = [self._owned_item_from_row(row) for row in rows[: query.limit]]
        next_cursor = (
            self._encode_cursor(items[-1], query.sort)
            if has_more and items
            else None
        )
        return OwnedSharedGuidePage(items=items, next_cursor=next_cursor)

    @staticmethod
    def _identity(identity: Any) -> tuple[str, int, str]:
        try:
            return (
                str(identity.share_id),
                int(identity.index_version),
                str(identity.content_hash),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("identities must expose share_id, index_version, and content_hash") from exc

    def bulk_get_ready(
        self,
        identities: Sequence[Any],
        exclude_session_id: str | None = None,
    ) -> list[SharedGuideRecord]:
        keys = [self._identity(identity) for identity in identities]
        if not keys:
            return []
        predicates = [
            and_(
                self.share_table.c.share_id == share_id,
                self.share_table.c.index_version == index_version,
                self.share_table.c.content_hash == content_hash,
            )
            for share_id, index_version, content_hash in keys
        ]
        statement = select(self.share_table).where(
            self.share_table.c.publication_status == PublicationStatus.PUBLIC.value,
            self.share_table.c.index_status == ShareIndexStatus.READY.value,
            or_(*predicates),
        )
        if exclude_session_id is not None:
            statement = statement.where(
                self.share_table.c.source_session_id != exclude_session_id
            )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        records = [self._record_from_row(row) for row in rows]
        by_key = {
            (record.share_id, record.index_version, record.content_hash): record
            for record in records
        }
        return [by_key[key] for key in keys if key in by_key]
