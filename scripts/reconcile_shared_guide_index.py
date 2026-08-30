"""Compare authoritative MySQL shared guides with Qdrant without recreating it."""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import models

from app.core.config import settings
from app.persistence.database import MySQLDatabaseConfig, create_mysql_engine
from app.rag.embedding import DashScopeEmbeddingClient
from app.rag.qdrant_index import (
    QdrantSharedGuideIndex,
    create_qdrant_client,
    validate_collection_name,
)
from app.sharing.models import PublicationStatus, ShareIndexStatus
from scripts.reindex_shared_guides import (
    CollectionSchemaMismatchError,
    MySQLMaintenanceStore,
    MaintenanceStorageError,
    _batch_size,
    _is_active_public,
    cleanup_stale_upsert,
    index_payload,
    iter_active_public,
    read_authoritative_record,
    utc_now,
    validate_existing_collection,
)


@dataclass(frozen=True, slots=True)
class IndexedPoint:
    share_id: str | None
    index_version: int | None
    content_hash: str | None
    point_id: object | None = None
    malformed_reason: str | None = None
    visibility: object | None = None


class ConditionalDeleteOutcome(str, Enum):
    """Outcome of a server-side conditional delete plus physical-point confirmation."""

    DELETED = "deleted"
    UNCHANGED = "unchanged"
    REPLACED = "replaced"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class ReconcileReport:
    dry_run: bool
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)
    wrong_id: list[str] = field(default_factory=list)
    inconsistent: list[str] = field(default_factory=list)
    repaired: int = 0
    deleted: int = 0
    skipped: int = 0
    requeued: int = 0
    failed: int = 0
    schema_mismatch: bool = False

    @property
    def exit_code(self) -> int:
        return 1 if self.failed or self.schema_mismatch else 0


class QdrantMaintenanceIndex:
    """Public-SDK scroll/delete adapter; collection lifecycle is intentionally absent."""

    def __init__(self, *, client: Any, collection: str, dimension: int) -> None:
        self.client = client
        self.collection = collection
        self.vector_index = QdrantSharedGuideIndex(
            client=client,
            collection=collection,
            dimension=dimension,
        )

    def scroll(self, *, collection: str, offset: object | None, limit: int):
        points, next_offset = self.client.scroll(
            collection_name=collection,
            offset=offset,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        identities: list[IndexedPoint] = []
        for point in points:
            identities.append(
                self._identity(
                    getattr(point, "payload", None),
                    point_id=getattr(point, "id", None),
                )
            )
        return identities, next_offset

    def upsert(self, share_id, vector, *, payload):
        self.vector_index.upsert(share_id, vector, payload=payload)

    def read_point(
        self,
        *,
        collection: str,
        point_id: object,
    ) -> IndexedPoint | None:
        points = self.client.retrieve(
            collection_name=collection,
            ids=[point_id],
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        point = points[0]
        return self._identity(
            getattr(point, "payload", None),
            point_id=getattr(point, "id", None),
        )

    def delete_point(
        self,
        *,
        collection: str,
        point: IndexedPoint,
    ) -> ConditionalDeleteOutcome:
        outcome = self.delete_point_if_matches(collection=collection, point=point)
        if outcome is ConditionalDeleteOutcome.UNAVAILABLE:
            raise ValueError("point payload is not complete enough for conditional deletion")
        return outcome

    def delete_point_if_matches(
        self,
        *,
        collection: str,
        point: IndexedPoint,
    ) -> ConditionalDeleteOutcome:
        point_id = point.point_id
        if (
            point_id is None
            or point.malformed_reason is not None
            or point.share_id is None
            or point.index_version is None
            or point.content_hash is None
            or (
                point.visibility is not None
                and not isinstance(point.visibility, str)
            )
        ):
            return ConditionalDeleteOutcome.UNAVAILABLE
        conditions = [
            models.HasIdCondition(has_id=[point_id]),
            models.FieldCondition(
                key="share_id",
                match=models.MatchValue(value=point.share_id),
            ),
            models.FieldCondition(
                key="index_version",
                match=models.MatchValue(value=point.index_version),
            ),
            models.FieldCondition(
                key="content_hash",
                match=models.MatchValue(value=point.content_hash),
            ),
        ]
        if point.visibility is not None:
            conditions.append(
                models.FieldCondition(
                    key="visibility",
                    match=models.MatchValue(value=point.visibility),
                )
            )
        self.client.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(must=conditions),
            ),
            wait=True,
        )
        current = self.read_point(collection=collection, point_id=point_id)
        if current is None:
            return ConditionalDeleteOutcome.DELETED
        if (
            _physical_id(current) == point_id
            and current.share_id == point.share_id
            and current.index_version == point.index_version
            and current.content_hash == point.content_hash
            and current.visibility == point.visibility
            and current.malformed_reason == point.malformed_reason
        ):
            return ConditionalDeleteOutcome.UNCHANGED
        return ConditionalDeleteOutcome.REPLACED

    def delete_identity(self, share_id: str, *, index_version: int) -> None:
        self.vector_index.delete(share_id, index_version=index_version)

    @staticmethod
    def _identity(payload: object, *, point_id: object | None = None) -> IndexedPoint:
        if not isinstance(payload, Mapping):
            return IndexedPoint(
                share_id=None,
                index_version=None,
                content_hash=None,
                point_id=point_id,
                malformed_reason="payload is not an object",
                visibility=None,
            )
        share_id = payload.get("share_id")
        index_version = payload.get("index_version")
        content_hash = payload.get("content_hash")
        visibility = payload.get("visibility")
        visibility_present = "visibility" in payload
        try:
            normalized_id = str(UUID(str(share_id)))
        except (ValueError, TypeError, AttributeError):
            return IndexedPoint(
                share_id=None,
                index_version=None,
                content_hash=None,
                point_id=point_id,
                malformed_reason="payload share_id is not a UUID",
                visibility=visibility,
            )
        if isinstance(index_version, bool) or not isinstance(index_version, int) or index_version < 1:
            return IndexedPoint(
                share_id=normalized_id,
                index_version=None,
                content_hash=None,
                point_id=point_id,
                malformed_reason="payload index_version is invalid",
                visibility=visibility,
            )
        if (
            not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in content_hash)
        ):
            return IndexedPoint(
                share_id=normalized_id,
                index_version=index_version,
                content_hash=None,
                point_id=point_id,
                malformed_reason="payload content_hash is invalid",
                visibility=visibility,
            )
        if visibility_present and not isinstance(visibility, str):
            return IndexedPoint(
                share_id=normalized_id,
                index_version=index_version,
                content_hash=content_hash,
                point_id=point_id,
                malformed_reason="payload visibility is invalid",
                visibility=visibility,
            )
        return IndexedPoint(
            share_id=normalized_id,
            index_version=index_version,
            content_hash=content_hash,
            point_id=point_id,
            visibility=visibility,
        )


@dataclass(slots=True)
class RuntimeDependencies:
    store: MySQLMaintenanceStore
    embedding_client: DashScopeEmbeddingClient | None
    vector_index: QdrantMaintenanceIndex
    validate_schema: Callable[[], None]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report and optionally repair shared-guide index drift",
    )
    parser.add_argument("--apply", action="store_true", help="repair missing and stale points")
    parser.add_argument(
        "--delete-extra",
        action="store_true",
        help="delete extra points; requires --apply",
    )
    parser.add_argument("--batch-size", type=_batch_size, default=100)
    args = parser.parse_args(argv)
    if args.delete_extra and not args.apply:
        parser.error("--delete-extra requires --apply")
    return args


def _physical_id(point: IndexedPoint) -> object | None:
    return point.point_id if point.point_id is not None else point.share_id


def _display_point_id(point: IndexedPoint) -> str:
    return str(_physical_id(point))


def _physical_id_matches(point: IndexedPoint) -> bool:
    if point.share_id is None:
        return False
    physical_id = _physical_id(point)
    try:
        return str(UUID(str(physical_id))) == point.share_id
    except (TypeError, ValueError, AttributeError):
        return False


def _point_matches_record(point: IndexedPoint, record) -> bool:
    return bool(
        point.share_id == record.share_id
        and point.index_version == record.index_version
        and point.content_hash == record.content_hash
    )


def _read_points(
    vector_index,
    *,
    collection: str,
    batch_size: int,
) -> tuple[dict[str, IndexedPoint], list[IndexedPoint], list[IndexedPoint]]:
    points: dict[str, IndexedPoint] = {}
    malformed: list[IndexedPoint] = []
    wrong_id: list[IndexedPoint] = []
    offset: object | None = None
    while True:
        page, next_offset = vector_index.scroll(
            collection=collection,
            offset=offset,
            limit=batch_size,
        )
        for point in page:
            if point.malformed_reason is not None or point.share_id is None:
                malformed.append(point)
                continue
            if point.index_version is None or point.content_hash is None:
                malformed.append(point)
                continue
            if point.share_id in points:
                malformed.append(
                    IndexedPoint(
                        share_id=point.share_id,
                        index_version=point.index_version,
                        content_hash=point.content_hash,
                        point_id=point.point_id,
                        malformed_reason="duplicate payload share_id",
                        visibility=point.visibility,
                    )
                )
                continue
            points[point.share_id] = point
            if not _physical_id_matches(point):
                wrong_id.append(point)
        if next_offset is None:
            return points, malformed, wrong_id
        if next_offset == offset:
            raise RuntimeError("Qdrant scroll cursor did not advance")
        offset = next_offset


def _can_delete_extra(store, point: IndexedPoint) -> bool:
    """Allow deletion only while the observed point is still non-authoritative."""

    if point.share_id is None:
        return True
    try:
        current = store.get_index_record(point.share_id)
    except Exception as error:
        raise MaintenanceStorageError(
            "authoritative shared-guide delete guard read failed"
        ) from error
    if current is None:
        return True
    if (
        current.publication_status is PublicationStatus.UNPUBLISHED
        and current.index_status is ShareIndexStatus.DELETED
    ):
        return _point_matches_record(point, current)
    if not _is_active_public(current):
        return False
    return _point_matches_record(point, current) and not _physical_id_matches(point)


def _point_snapshot_unchanged(vector_index, *, collection: str, point: IndexedPoint) -> bool:
    """Re-read the physical point before delete to close repair/cleanup TOCTOU."""

    point_id = _physical_id(point)
    reader = getattr(vector_index, "read_point", None)
    if point_id is None or reader is None:
        return False
    current = reader(collection=collection, point_id=point_id)
    if current is None:
        return False
    return bool(
        _physical_id(current) == point_id
        and current.share_id == point.share_id
        and current.index_version == point.index_version
        and current.content_hash == point.content_hash
        and current.visibility == point.visibility
        and current.malformed_reason == point.malformed_reason
    )


def _requeue_after_delete(store, point: IndexedPoint, *, now) -> tuple[bool, bool]:
    if point.share_id is None:
        return False, False
    try:
        current = store.get_index_record(point.share_id)
    except Exception as error:
        raise MaintenanceStorageError(
            "authoritative shared-guide post-delete read failed"
        ) from error
    if not _is_active_public(current):
        return False, False
    try:
        requeued = bool(
            store.requeue_current_upsert(
                current.share_id,
                current.index_version,
                current.content_hash,
                now=now,
            )
        )
        return True, requeued
    except Exception as error:
        raise MaintenanceStorageError(
            "authoritative shared-guide post-delete requeue failed"
        ) from error


def _cleanup_candidates(
    points: dict[str, IndexedPoint],
    malformed: list[IndexedPoint],
    wrong_id: list[IndexedPoint],
    extra: list[str],
) -> list[IndexedPoint]:
    candidates = [*malformed, *wrong_id, *(points[share_id] for share_id in extra)]
    unique: list[IndexedPoint] = []
    seen: set[tuple[str, str]] = set()
    for point in candidates:
        key = (type(_physical_id(point)).__name__, repr(_physical_id(point)))
        if key not in seen:
            seen.add(key)
            unique.append(point)
    return unique


def run_reconcile(
    args: argparse.Namespace,
    *,
    collection: str,
    store,
    embedding_client,
    vector_index,
    output: TextIO = sys.stdout,
    validate_schema: Callable[[], None] | None = None,
) -> ReconcileReport:
    collection = validate_collection_name(collection)
    report = ReconcileReport(dry_run=not args.apply)
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

    records: dict[str, Any] = {}
    for record in iter_active_public(store, batch_size=args.batch_size):
        if _is_active_public(record):
            records[record.share_id] = record
        else:
            report.inconsistent.append(record.share_id)
    report.failed += len(report.inconsistent)

    points, malformed_points, wrong_id_points = _read_points(
        vector_index,
        collection=collection,
        batch_size=args.batch_size,
    )
    missing = sorted(set(records).difference(points))
    stale = sorted(
        share_id
        for share_id in set(records).intersection(points)
        if (
            records[share_id].index_version != points[share_id].index_version
            or records[share_id].content_hash != points[share_id].content_hash
        )
    )
    extra = sorted(set(points).difference(records))
    report.missing = missing
    report.stale = stale
    report.extra = extra
    report.malformed = sorted(_display_point_id(point) for point in malformed_points)
    report.wrong_id = sorted(_display_point_id(point) for point in wrong_id_points)
    print(
        f"reconcile mode={'APPLY' if args.apply else 'DRY-RUN'} collection={collection}",
        file=output,
    )
    print(
        f"missing={len(missing)} stale={len(stale)} extra={len(extra)} "
        f"malformed={len(report.malformed)} wrong_id={len(report.wrong_id)} "
        f"inconsistent={len(report.inconsistent)}",
        file=output,
    )
    for category, share_ids in (
        ("missing", missing),
        ("stale", stale),
        ("extra", extra),
        ("malformed", report.malformed),
        ("wrong_id", report.wrong_id),
        ("inconsistent", report.inconsistent),
    ):
        for item_id in share_ids:
            label = "point_id" if category in {"malformed", "wrong_id"} else "share_id"
            print(f"{category} {label}={item_id}", file=output)

    if args.apply:
        if embedding_client is None:
            raise ValueError("apply mode requires an embedding adapter")
        for share_id in missing + stale:
            record = records[share_id]
            try:
                authoritative = read_authoritative_record(store, record)
                if authoritative is None:
                    report.skipped += 1
                    print(
                        f"share_id={share_id} repair=skipped reason=authoritative_record_changed_before_embedding",
                        file=output,
                    )
                    continue
                vector = embedding_client.embed(authoritative.retrieval_text)
                if len(vector) != record.embedding_dimension or not all(
                    math.isfinite(float(value)) for value in vector
                ):
                    raise ValueError("invalid embedding returned during reconciliation")
                authoritative = read_authoritative_record(store, record)
                if authoritative is None:
                    report.skipped += 1
                    print(
                        f"share_id={share_id} repair=skipped reason=authoritative_record_changed_before_upsert",
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
                        now=utc_now(),
                    )
                    raise RuntimeError(
                        "authoritative record changed after reconcile repair"
                    )
                report.repaired += 1
            except Exception as error:
                report.failed += 1
                print(
                    f"share_id={share_id} repair=failed error_class={type(error).__name__}",
                    file=output,
                )
        if args.delete_extra:
            cleanup_points = _cleanup_candidates(
                points,
                malformed_points,
                wrong_id_points,
                extra,
            )
            for point in cleanup_points:
                point_id = _display_point_id(point)
                try:
                    if not _point_snapshot_unchanged(
                        vector_index,
                        collection=collection,
                        point=point,
                    ):
                        report.skipped += 1
                        print(
                            f"point_id={point_id} delete=skipped "
                            "reason=physical_point_changed_after_scroll",
                            file=output,
                        )
                        continue
                    if not _can_delete_extra(store, point):
                        report.skipped += 1
                        print(
                            f"point_id={point_id} delete=skipped reason=authoritative_record_changed",
                            file=output,
                        )
                        continue
                    conditional_delete = getattr(
                        vector_index,
                        "delete_point_if_matches",
                        None,
                    )
                    if conditional_delete is None:
                        report.skipped += 1
                        print(
                            f"point_id={point_id} delete=skipped "
                            "reason=conditional_delete_unavailable",
                            file=output,
                        )
                        continue
                    delete_outcome = conditional_delete(
                        collection=collection,
                        point=point,
                    )
                    if delete_outcome is True or delete_outcome == ConditionalDeleteOutcome.DELETED:
                        report.deleted += 1
                        active_after_delete, requeued = _requeue_after_delete(
                            store,
                            point,
                            now=utc_now(),
                        )
                        if active_after_delete:
                            report.failed += 1
                            if requeued:
                                report.requeued += 1
                            print(
                                f"point_id={point_id} delete=concurrent_authoritative_reappearance "
                                f"requeued={str(requeued).lower()}",
                                file=output,
                            )
                        continue
                    if delete_outcome == ConditionalDeleteOutcome.UNCHANGED:
                        report.failed += 1
                        print(
                            f"point_id={point_id} delete=failed "
                            "reason=conditional_delete_not_confirmed",
                            file=output,
                        )
                        continue
                    if delete_outcome == ConditionalDeleteOutcome.REPLACED:
                        report.skipped += 1
                        print(
                            f"point_id={point_id} delete=skipped "
                            "reason=physical_point_replaced_after_conditional_delete",
                            file=output,
                        )
                        continue
                    report.skipped += 1
                    reason = (
                        "conditional_delete_unavailable"
                        if delete_outcome == ConditionalDeleteOutcome.UNAVAILABLE
                        else "physical_point_changed_before_conditional_delete"
                    )
                    print(
                        f"point_id={point_id} delete=skipped reason={reason}",
                        file=output,
                    )
                except Exception as error:
                    report.failed += 1
                    print(
                        f"point_id={point_id} delete=failed error_class={type(error).__name__}",
                        file=output,
                    )
    print(
        f"summary repaired={report.repaired} deleted={report.deleted} "
        f"skipped={report.skipped} requeued={report.requeued} failed={report.failed}",
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
            dimension=int(settings.EMBEDDING_DIMENSION or 768),
            timeout_seconds=float(settings.EMBEDDING_TIMEOUT_SECONDS or 10),
            max_attempts=int(settings.EMBEDDING_MAX_ATTEMPTS or 3),
        )
    return RuntimeDependencies(
        store=store,
        embedding_client=embedding,
        vector_index=QdrantMaintenanceIndex(
            client=client,
            collection=collection,
            dimension=768,
        ),
        validate_schema=lambda: validate_existing_collection(client, collection),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        dependencies = build_runtime_dependencies(apply=args.apply)
        report = run_reconcile(
            args,
            collection=settings.QDRANT_COLLECTION,
            store=dependencies.store,
            embedding_client=dependencies.embedding_client,
            vector_index=dependencies.vector_index,
            validate_schema=dependencies.validate_schema,
        )
        return report.exit_code
    except Exception as error:
        print(f"reconcile unavailable error_class={type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
