"""Safely rebuild active public shared-guide vectors in the configured collection."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, TextIO
from uuid import UUID, uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError
from qdrant_client import models
from sqlalchemy import select

from app.core.config import settings
from app.persistence.database import MySQLDatabaseConfig, create_mysql_engine
from app.persistence.mysql_base import as_utc
from app.rag.embedding import DashScopeEmbeddingClient
from app.rag.qdrant_index import (
    QdrantSharedGuideIndex,
    create_qdrant_client,
    validate_collection_name,
)
from app.rag.text_builder import EmbeddingTextBuilder
from app.sharing.exceptions import SharedGuideConflictError
from app.sharing.models import (
    IndexOperation,
    PublicationStatus,
    ShareIndexStatus,
    SharePublishDraft,
    SharedGuideRecord,
    SharedGuideSnapshot,
    utc_now,
)
from app.sharing.mysql_store import MySQLSharedGuideStore


_MAX_BATCH_SIZE = 1000


class CollectionSchemaMismatchError(RuntimeError):
    """The configured existing collection is not safe for shared-guide V1."""


class MaintenanceConcurrencyError(RuntimeError):
    """The authoritative row changed around a maintenance vector operation."""


class MaintenanceStorageError(RuntimeError):
    """An authoritative maintenance read or recovery write failed."""


class ActivePublicStore(Protocol):
    def list_active_public(
        self,
        *,
        after_share_id: str | None,
        limit: int,
        share_id: str | None = None,
    ) -> list[SharedGuideRecord]: ...


class MySQLMaintenanceStore(MySQLSharedGuideStore):
    """Operations-only keyset reader layered on the transactional business store."""

    def list_active_public(
        self,
        *,
        after_share_id: str | None,
        limit: int,
        share_id: str | None = None,
    ) -> list[SharedGuideRecord]:
        if not 1 <= limit <= _MAX_BATCH_SIZE:
            raise ValueError("batch size must be between 1 and 1000")
        statement = (
            select(self.share_table)
            .where(
                self.share_table.c.publication_status == PublicationStatus.PUBLIC.value,
                self.share_table.c.index_status == ShareIndexStatus.READY.value,
            )
            .order_by(self.share_table.c.share_id)
            .limit(limit)
        )
        if after_share_id is not None:
            statement = statement.where(self.share_table.c.share_id > after_share_id)
        if share_id is not None:
            statement = statement.where(self.share_table.c.share_id == share_id)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._maintenance_record_from_row(row) for row in rows]

    @classmethod
    def _maintenance_record_from_row(cls, row: Mapping[str, object]) -> SharedGuideRecord:
        """Keep timestamp-corrupt PUBLIC+READY rows visible to the operator.

        The business model intentionally rejects these records.  Operations must
        still report them instead of allowing a validation exception to erase the
        row from a bounded maintenance page.
        """

        try:
            return cls._record_from_row(row)
        except ValidationError:
            if (
                row["publication_status"] != PublicationStatus.PUBLIC.value
                or row["index_status"] != ShareIndexStatus.READY.value
                or (row.get("published_at") is not None and row.get("indexed_at") is not None)
            ):
                raise
            preferences = row["preferences_json"]
            if isinstance(preferences, str):
                preferences = json.loads(preferences)
            snapshot = row["snapshot_json"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            return SharedGuideRecord.model_construct(
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
                preferences=preferences,
                snapshot=SharedGuideSnapshot.model_validate(snapshot),
                retrieval_text=row["retrieval_text"],
                content_hash=row["content_hash"],
                quality_level=row["quality_level"],
                quality_score=row["quality_score"],
                publication_status=PublicationStatus(row["publication_status"]),
                index_status=ShareIndexStatus(row["index_status"]),
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


@dataclass(slots=True)
class ReindexReport:
    dry_run: bool
    selected: int = 0
    rebuilt_same_version: int = 0
    changed_hash: int = 0
    reindexed: int = 0
    skipped: int = 0
    inconsistent: list[str] = field(default_factory=list)
    failed: int = 0
    schema_mismatch: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.failed or self.schema_mismatch else 0


@dataclass(slots=True)
class RuntimeDependencies:
    store: MySQLMaintenanceStore
    text_builder: EmbeddingTextBuilder
    embedding_client: DashScopeEmbeddingClient | None
    vector_index: QdrantSharedGuideIndex | None
    validate_schema: Callable[[], None]


def _batch_size(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("batch size must be an integer") from None
    if not 1 <= parsed <= _MAX_BATCH_SIZE:
        raise argparse.ArgumentTypeError("batch size must be between 1 and 1000")
    return parsed


def _share_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("share id must be a UUID") from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild active PUBLIC + READY shared-guide vectors",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform writes; omitted means deterministic dry-run",
    )
    parser.add_argument("--batch-size", type=_batch_size, default=100)
    parser.add_argument("--share-id", type=_share_id, default=None)
    return parser.parse_args(argv)


def validate_existing_collection(
    client,
    collection: str,
    *,
    expected_dimension: int = 768,
) -> None:
    """Validate an existing collection through Qdrant's public client API."""

    collection = validate_collection_name(collection)
    if not client.collection_exists(collection):
        raise CollectionSchemaMismatchError("configured Qdrant collection does not exist")
    details = client.get_collection(collection_name=collection)
    vectors = getattr(getattr(getattr(details, "config", None), "params", None), "vectors", None)
    if not isinstance(vectors, models.VectorParams):
        raise CollectionSchemaMismatchError("collection must use one unnamed vector")
    if vectors.size != expected_dimension or vectors.distance != models.Distance.COSINE:
        raise CollectionSchemaMismatchError(
            f"collection vector schema must be {expected_dimension}-dimensional cosine"
        )


def _is_active_public(record: SharedGuideRecord | None) -> bool:
    return (
        record is not None
        and record.publication_status is PublicationStatus.PUBLIC
        and record.index_status is ShareIndexStatus.READY
        and record.published_at is not None
        and record.indexed_at is not None
    )


def _is_public_ready_state(record: SharedGuideRecord) -> bool:
    return (
        record.publication_status is PublicationStatus.PUBLIC
        and record.index_status is ShareIndexStatus.READY
    )


def iter_active_public(
    store: ActivePublicStore,
    *,
    batch_size: int,
    share_id: str | None = None,
) -> Iterable[SharedGuideRecord]:
    after_share_id: str | None = None
    while True:
        page = store.list_active_public(
            after_share_id=after_share_id,
            limit=batch_size,
            share_id=share_id,
        )
        if not page:
            return
        for record in page:
            if _is_public_ready_state(record):
                yield record
        if share_id is not None or len(page) < batch_size:
            return
        next_cursor = page[-1].share_id
        if next_cursor == after_share_id:
            raise RuntimeError("MySQL pagination cursor did not advance")
        after_share_id = next_cursor


def _same_authoritative_identity(
    expected: SharedGuideRecord,
    current: SharedGuideRecord | None,
) -> bool:
    return bool(
        current is not None
        and _is_active_public(current)
        and current.share_id == expected.share_id
        and current.index_version == expected.index_version
        and current.content_hash == expected.content_hash
        and current.published_at == expected.published_at
        and current.indexed_at == expected.indexed_at
    )


def read_authoritative_record(store, expected: SharedGuideRecord) -> SharedGuideRecord | None:
    """Read and compare the exact current MySQL target immediately around a write."""

    try:
        current = store.get_index_record(expected.share_id)
    except Exception as error:
        raise MaintenanceStorageError(
            "authoritative shared-guide read failed"
        ) from error
    return current if _same_authoritative_identity(expected, current) else None


def _read_current_record(store, share_id: str) -> SharedGuideRecord | None:
    try:
        return store.get_index_record(share_id)
    except Exception as error:
        raise MaintenanceStorageError(
            "authoritative shared-guide recovery read failed"
        ) from error


def cleanup_stale_upsert(
    store,
    vector_index,
    expected: SharedGuideRecord,
    *,
    now: datetime,
) -> None:
    """Remove a version-filtered maintenance write and requeue a new current row."""

    try:
        delete = getattr(vector_index, "delete", None)
        if delete is None:
            delete = getattr(vector_index, "delete_identity")
        delete(expected.share_id, index_version=expected.index_version)
    except Exception as error:
        raise MaintenanceStorageError(
            "stale maintenance vector cleanup failed"
        ) from error
    current = _read_current_record(store, expected.share_id)
    if _is_active_public(current):
        try:
            store.requeue_current_upsert(
                current.share_id,
                current.index_version,
                current.content_hash,
                now=now,
            )
        except Exception as error:
            raise MaintenanceStorageError(
                "stale maintenance requeue failed"
            ) from error


def index_payload(record: SharedGuideRecord) -> dict[str, object]:
    if record.published_at is None:
        raise ValueError("active public shared guide has no publication timestamp")
    return {
        "share_id": record.share_id,
        "city": record.city_normalized,
        "travel_days": record.travel_days,
        "transportation": record.transportation,
        "visibility": "PUBLIC",
        "quality_score": record.quality_score if record.quality_score is not None else 0.0,
        "published_at": int(record.published_at.timestamp()),
        "index_version": record.index_version,
        "content_hash": record.content_hash,
    }


def _draft_with_canonical_text(record: SharedGuideRecord, built) -> SharePublishDraft:
    values = {
        name: getattr(record, name)
        for name in SharePublishDraft.model_fields
    }
    values.update(
        retrieval_text=built.text,
        content_hash=built.content_hash,
        city_normalized=built.city_normalized,
        transportation=built.transportation_normalized,
        retrieval_template_version=built.template_version,
    )
    return SharePublishDraft.model_validate(values)


def _apply_changed_record(
    record: SharedGuideRecord,
    built,
    *,
    store,
    embedding_client,
    vector_index,
    now: datetime,
    lease_seconds: float,
) -> None:
    intent = store.stage_update(
        record.share_id,
        record.author_user_id,
        _draft_with_canonical_text(record, built),
        now=now,
        allow_active_upsert_supersede=False,
        expected_index_version=record.index_version,
        expected_content_hash=record.content_hash,
        expected_published_at=record.published_at,
        expected_indexed_at=record.indexed_at,
    )
    if intent.job is None or not intent.operation_required:
        raise RuntimeError("changed reindex did not create an UPSERT intent")
    worker_id = f"reindex:{uuid4()}"
    claimed = store.claim_index_job(
        intent.job.job_id,
        worker_id,
        now=now,
        lease_seconds=lease_seconds,
    )
    if claimed is None:
        raise RuntimeError("reindex UPSERT job could not be claimed")
    try:
        vector = embedding_client.embed(intent.record.retrieval_text)
        vector_index.upsert(
            intent.record.share_id,
            vector,
            payload=index_payload(intent.record),
        )
        completed = store.complete_index_operation(
            intent.job.job_id,
            intent.record.share_id,
            intent.record.index_version,
            IndexOperation.UPSERT,
            worker_id=worker_id,
            now=now,
        )
        if not completed:
            raise RuntimeError("reindex UPSERT completion lost compare-and-set")
    except Exception as error:
        try:
            store.record_index_failure(
                intent.job.job_id,
                intent.record.share_id,
                intent.record.index_version,
                IndexOperation.UPSERT,
                error,
                worker_id=worker_id,
                next_retry_at=now + timedelta(seconds=2),
                terminal=False,
                now=now,
            )
        except Exception:
            pass
        raise


def run_reindex(
    args: argparse.Namespace,
    *,
    collection: str,
    store,
    text_builder,
    embedding_client,
    vector_index,
    clock=utc_now,
    output: TextIO = sys.stdout,
    lease_seconds: float = 120.0,
    validate_schema: Callable[[], None] | None = None,
) -> ReindexReport:
    collection = validate_collection_name(collection)
    report = ReindexReport(dry_run=not args.apply)
    if validate_schema is not None:
        try:
            validate_schema()
        except CollectionSchemaMismatchError as error:
            report.schema_mismatch = True
            report.failed += 1
            print(
                f"schema_mismatch error_class={type(error).__name__}",
                file=output,
            )
            return report
    if args.apply and (embedding_client is None or vector_index is None):
        raise ValueError("apply mode requires embedding and Qdrant adapters")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"reindex mode={mode} collection={collection}", file=output)
    for record in iter_active_public(
        store,
        batch_size=args.batch_size,
        share_id=args.share_id,
    ):
        report.selected += 1
        if not _is_active_public(record):
            report.inconsistent.append(record.share_id)
            report.failed += 1
            print(
                f"share_id={record.share_id} outcome=inconsistent reason=missing_publication_or_index_timestamp",
                file=output,
            )
            continue
        try:
            built = text_builder.build_document(record.snapshot)
            changed = built.content_hash != record.content_hash
            if changed:
                report.changed_hash += 1
            if not args.apply:
                print(
                    f"share_id={record.share_id} action={'versioned-reindex' if changed else 'same-version-rebuild'}",
                    file=output,
                )
                continue
            if changed:
                _apply_changed_record(
                    record,
                    built,
                    store=store,
                    embedding_client=embedding_client,
                    vector_index=vector_index,
                    now=clock(),
                    lease_seconds=lease_seconds,
                )
                report.reindexed += 1
            else:
                if read_authoritative_record(store, record) is None:
                    report.skipped += 1
                    print(
                        f"share_id={record.share_id} outcome=skipped reason=authoritative_record_changed_before_embedding",
                        file=output,
                    )
                    continue
                vector = embedding_client.embed(built.text)
                authoritative = read_authoritative_record(store, record)
                if authoritative is None:
                    report.skipped += 1
                    print(
                        f"share_id={record.share_id} outcome=skipped reason=authoritative_record_changed_before_upsert",
                        file=output,
                    )
                    continue
                vector_index.upsert(
                    authoritative.share_id,
                    vector,
                    payload=index_payload(authoritative),
                )
                if read_authoritative_record(store, authoritative) is None:
                    cleanup_stale_upsert(
                        store,
                        vector_index,
                        authoritative,
                        now=clock(),
                    )
                    raise MaintenanceConcurrencyError(
                        "authoritative record changed after same-version rebuild"
                    )
                report.rebuilt_same_version += 1
        except SharedGuideConflictError:
            report.skipped += 1
            print(
                f"share_id={record.share_id} outcome=skipped "
                "reason=authoritative_record_changed_before_stage",
                file=output,
            )
        except Exception as error:
            report.failed += 1
            print(
                f"share_id={record.share_id} outcome=failed error_class={type(error).__name__}",
                file=output,
            )
    print(
        "summary "
        f"selected={report.selected} same_version={report.rebuilt_same_version} "
        f"changed_hash={report.changed_hash} reindexed={report.reindexed} "
        f"skipped={report.skipped} inconsistent={len(report.inconsistent)} "
        f"schema_mismatch={report.schema_mismatch} failed={report.failed}",
        file=output,
    )
    return report


def build_runtime_dependencies(*, apply: bool) -> RuntimeDependencies:
    if settings.DATABASE_BACKEND != "mysql":
        raise RuntimeError("maintenance commands require DATABASE_BACKEND=mysql")
    collection = validate_collection_name(settings.QDRANT_COLLECTION)
    engine = create_mysql_engine(MySQLDatabaseConfig.from_settings(settings))
    store = MySQLMaintenanceStore(engine)
    client = create_qdrant_client(
        url=settings.QDRANT_URL,
        api_key=settings.QDRANT_API_KEY,
        timeout_seconds=float(settings.QDRANT_TIMEOUT_SECONDS or 5),
    )
    index = QdrantSharedGuideIndex(
        client=client,
        collection=collection,
        dimension=768,
    )
    embedding = None
    if apply:
        if settings.EMBEDDING_MODEL != "qwen3.7-text-embedding":
            raise RuntimeError(
                "EMBEDDING_MODEL must be qwen3.7-text-embedding in apply mode"
            )
        if not settings.DASHSCOPE_API_KEY:
            raise RuntimeError("DASHSCOPE_API_KEY is required in apply mode")
        if not settings.DASHSCOPE_BASE_URL:
            raise RuntimeError("DASHSCOPE_BASE_URL is required in apply mode")
        embedding = DashScopeEmbeddingClient(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=settings.DASHSCOPE_BASE_URL,
            model=settings.EMBEDDING_MODEL,
            dimension=768,
            timeout_seconds=float(settings.EMBEDDING_TIMEOUT_SECONDS or 10),
            max_attempts=int(settings.EMBEDDING_MAX_ATTEMPTS or 3),
        )
    return RuntimeDependencies(
        store,
        EmbeddingTextBuilder(),
        embedding,
        index,
        lambda: validate_existing_collection(client, collection),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        dependencies = build_runtime_dependencies(apply=args.apply)
        report = run_reindex(
            args,
            collection=settings.QDRANT_COLLECTION,
            store=dependencies.store,
            text_builder=dependencies.text_builder,
            embedding_client=dependencies.embedding_client,
            vector_index=dependencies.vector_index,
            lease_seconds=float(settings.SHARE_INDEX_LEASE_SECONDS or 120),
            validate_schema=dependencies.validate_schema,
        )
        return report.exit_code
    except Exception as error:
        print(f"reindex unavailable error_class={type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
