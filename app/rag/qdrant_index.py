"""Qdrant adapter for the rebuildable public shared-guide vector index."""

from __future__ import annotations

import math
import re
import uuid
from typing import Any, Mapping, Sequence

from qdrant_client import QdrantClient, models

from .models import RetrievalFilterStage, VectorHit


class QdrantSchemaMismatchError(RuntimeError):
    """An existing collection cannot safely hold this application's vectors."""


APPROVED_PAYLOAD_FIELDS = frozenset(
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
    }
)

_PAYLOAD_INDEXES = (
    ("city", models.PayloadSchemaType.KEYWORD),
    ("travel_days", models.PayloadSchemaType.INTEGER),
    ("transportation", models.PayloadSchemaType.KEYWORD),
    ("visibility", models.PayloadSchemaType.KEYWORD),
)

_COLLECTION_NAME = re.compile(r"^shared_guide_embeddings_v[1-9][0-9]*$")


def validate_collection_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not _COLLECTION_NAME.fullmatch(normalized):
        raise ValueError(
            "QDRANT_COLLECTION must be an explicit shared_guide_embeddings_vN collection"
        )
    return normalized


def create_qdrant_client(*, url: str, api_key: str | None, timeout_seconds: float) -> QdrantClient:
    """Build the public Qdrant client without coupling runtime construction to the adapter."""

    return QdrantClient(url=url, api_key=api_key or None, timeout=timeout_seconds)


class QdrantSharedGuideIndex:
    """Collection bootstrap plus safe point operations for public guide embeddings."""

    VECTOR_DIMENSION = 768

    def __init__(self, *, client: Any, collection: str, dimension: int) -> None:
        if dimension != self.VECTOR_DIMENSION:
            raise ValueError("shared guide V1 requires a 768-dimensional vector")
        self._client = client
        self.collection = collection
        self.dimension = dimension

    def ensure_collection(self) -> None:
        if self._client.collection_exists(self.collection):
            self._validate_collection()
        else:
            try:
                self._client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=self.dimension,
                        distance=models.Distance.COSINE,
                    ),
                )
            except Exception as error:
                if not self._is_already_exists(error):
                    raise
                self._validate_collection()

        for field_name, field_schema in _PAYLOAD_INDEXES:
            try:
                self._client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field_name,
                    field_schema=field_schema,
                )
            except Exception as error:
                if not self._is_already_exists(error):
                    raise

    def upsert(
        self,
        share_id: str,
        vector: Sequence[float],
        *,
        payload: Mapping[str, object],
    ) -> None:
        point_id = uuid.UUID(share_id)
        missing_fields = APPROVED_PAYLOAD_FIELDS.difference(payload)
        if missing_fields:
            raise ValueError(
                "upsert payload is missing required fields: "
                + ", ".join(sorted(missing_fields))
            )
        try:
            payload_share_id = str(uuid.UUID(str(payload["share_id"])))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("upsert payload share_id must be a UUID") from None
        if payload_share_id != str(point_id):
            raise ValueError("upsert payload share_id does not match point id")
        if any(payload[field] is None for field in APPROVED_PAYLOAD_FIELDS):
            raise ValueError("upsert payload required fields cannot be null")
        point_payload = {
            key: value for key, value in payload.items() if key in APPROVED_PAYLOAD_FIELDS
        }
        point_payload["share_id"] = str(point_id)
        self._client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=list(vector),
                    payload=point_payload,
                )
            ],
            wait=True,
        )

    def delete(self, share_id: str, *, index_version: int) -> None:
        point_id = str(uuid.UUID(share_id))
        selector = models.FilterSelector(
            filter=models.Filter(
                must=[
                    self._match("share_id", point_id),
                    self._match("index_version", index_version),
                ]
            )
        )
        self._client.delete(
            collection_name=self.collection,
            points_selector=selector,
            wait=True,
        )

    def query(
        self,
        vector: Sequence[float],
        *,
        city: str,
        travel_days: int,
        transportation: str,
        stage: RetrievalFilterStage,
        limit: int,
        min_score: float,
        exclude_share_ids: Sequence[str] = (),
    ) -> list[VectorHit]:
        query_filter = self._query_filter(
            city=city,
            travel_days=travel_days,
            transportation=transportation,
            stage=stage,
            exclude_share_ids=exclude_share_ids,
        )
        response = self._client.query_points(
            collection_name=self.collection,
            query=list(vector),
            query_filter=query_filter,
            limit=limit,
            score_threshold=min_score,
            with_payload=True,
        )
        return [
            hit
            for point in response.points
            if (hit := self._vector_hit(point, stage)) is not None
        ]

    def _validate_collection(self) -> None:
        collection = self._client.get_collection(collection_name=self.collection)
        vectors = collection.config.params.vectors
        if not isinstance(vectors, models.VectorParams):
            raise QdrantSchemaMismatchError("collection must use one unnamed vector")
        if vectors.size != self.dimension or vectors.distance != models.Distance.COSINE:
            raise QdrantSchemaMismatchError("collection vector schema does not match configuration")

    def _query_filter(
        self,
        *,
        city: str,
        travel_days: int,
        transportation: str,
        stage: RetrievalFilterStage,
        exclude_share_ids: Sequence[str],
    ) -> models.Filter:
        must = [self._match("city", city), self._match("visibility", "PUBLIC")]
        if stage is RetrievalFilterStage.EXACT_DAYS_TRANSPORT:
            must.extend((self._match("travel_days", travel_days), self._match("transportation", transportation)))
        elif stage is RetrievalFilterStage.EXACT_DAYS:
            must.append(self._match("travel_days", travel_days))
        elif stage is RetrievalFilterStage.DAYS_PLUS_MINUS_ONE:
            must.append(
                models.FieldCondition(
                    key="travel_days",
                    range=models.Range(gte=max(1, travel_days - 1), lte=travel_days + 1),
                )
            )
        elif stage is not RetrievalFilterStage.SAME_CITY:
            raise ValueError(f"unsupported retrieval filter stage: {stage!r}")

        must_not = None
        if exclude_share_ids:
            must_not = [
                models.HasIdCondition(has_id=[uuid.UUID(share_id) for share_id in exclude_share_ids])
            ]
        return models.Filter(must=must, must_not=must_not)

    @staticmethod
    def _match(key: str, value: str | int) -> models.FieldCondition:
        return models.FieldCondition(key=key, match=models.MatchValue(value=value))

    @staticmethod
    def _is_already_exists(error: Exception) -> bool:
        return getattr(error, "status_code", None) == 409 or "already exist" in str(error).lower()

    @staticmethod
    def _vector_hit(point: Any, stage: RetrievalFilterStage) -> VectorHit | None:
        payload = getattr(point, "payload", None)
        score = getattr(point, "score", None)
        if not isinstance(payload, Mapping) or isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        if not math.isfinite(float(score)):
            return None
        share_id = payload.get("share_id")
        index_version = payload.get("index_version")
        content_hash = payload.get("content_hash")
        if isinstance(index_version, bool) or not isinstance(index_version, int) or index_version < 1:
            return None
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            return None
        if any(character not in "0123456789abcdefABCDEF" for character in content_hash):
            return None
        try:
            normalized_share_id = str(uuid.UUID(str(share_id)))
        except (TypeError, ValueError, AttributeError):
            return None
        return VectorHit(
            share_id=normalized_share_id,
            index_version=index_version,
            content_hash=content_hash,
            vector_score=float(score),
            filter_stage=stage,
        )
