"""Fail-open staged retrieval and deterministic shared-guide reranking."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from math import log1p
from typing import Callable, Sequence

from app.observability.rag_metrics import RagMetrics
from app.schemas.trip_schema import TripRequest
from app.sharing.models import (
    PublicationStatus,
    ShareIndexStatus,
    SharedGuideRecord,
    utc_now,
)

from .interfaces import (
    EmbeddingClient,
    SharedGuideReadyStore,
    SharedGuideVectorIndex,
)
from .models import (
    IndexedIdentity,
    RagContext,
    RagReference,
    RetrievalFilterStage,
    VectorHit,
)
from .text_builder import EmbeddingTextBuilder, _city, _clean, _transport, _unique


logger = logging.getLogger(__name__)

_STAGES = (
    RetrievalFilterStage.EXACT_DAYS_TRANSPORT,
    RetrievalFilterStage.EXACT_DAYS,
    RetrievalFilterStage.DAYS_PLUS_MINUS_ONE,
    RetrievalFilterStage.SAME_CITY,
)
_STAGE_RANK = {stage: index for index, stage in enumerate(_STAGES)}
EMBEDDING_DIMENSION = 768


def calculate_semantic_score(vector_score: float) -> float:
    return min(1.0, max(0.0, vector_score))


def calculate_quality_score(quality_score: float | None) -> float:
    value = 0.0 if quality_score is None else quality_score
    return min(1.0, max(0.0, value / 100.0))


def calculate_freshness_score(age_days: float) -> float:
    return 2.0 ** (-max(age_days, 0.0) / 180.0)


def calculate_like_score(like_count: int, max_like_count: int) -> float:
    if max_like_count == 0:
        return 0.0
    return log1p(like_count) / log1p(max_like_count)


def calculate_rerank_score(
    *,
    vector_score: float,
    quality_score: float | None,
    age_days: float,
    like_count: int,
    max_like_count: int,
) -> float:
    semantic = calculate_semantic_score(vector_score)
    quality = calculate_quality_score(quality_score)
    freshness = calculate_freshness_score(age_days)
    likes = calculate_like_score(like_count, max_like_count)
    return 0.90 * semantic + 0.07 * quality + 0.02 * freshness + 0.01 * likes


@dataclass(frozen=True)
class _RankedCandidate:
    record: SharedGuideRecord
    hit: VectorHit
    final_score: float
    reference: RagReference


class NoOpRagRetriever:
    def retrieve(
        self,
        request: TripRequest,
        *,
        exclude_session_id: str | None = None,
        selected_attractions: Sequence[str] = (),
    ) -> RagContext:
        del request, exclude_session_id, selected_attractions
        return RagContext(attempted=False, used=False, reason="disabled")


class RagRetrievalService:
    """Retrieve public same-city guides without making planning depend on RAG."""

    def __init__(
        self,
        *,
        embedding_client: EmbeddingClient,
        vector_index: SharedGuideVectorIndex,
        store: SharedGuideReadyStore,
        text_builder: EmbeddingTextBuilder,
        enabled: bool,
        embedding_model: str,
        top_k: int = 3,
        max_top_k: int = 5,
        candidate_limit: int = 20,
        min_score: float = 0.55,
        reference_max_chars: int = 6000,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        metrics: RagMetrics | None = None,
    ) -> None:
        self._validate_settings(
            embedding_model=embedding_model,
            top_k=top_k,
            max_top_k=max_top_k,
            candidate_limit=candidate_limit,
            min_score=min_score,
            reference_max_chars=reference_max_chars,
        )
        self._embedding_client = embedding_client
        self._vector_index = vector_index
        self._store = store
        self._text_builder = text_builder
        self._enabled = bool(enabled)
        self._embedding_model = embedding_model
        self._top_k = top_k
        self._candidate_limit = candidate_limit
        self._min_score = min_score
        self._reference_max_chars = reference_max_chars
        self._clock = clock
        self._monotonic = monotonic
        self._metrics = metrics
        self._template_version = getattr(
            text_builder,
            "TEMPLATE_VERSION",
            "retrieval_template_v1",
        )

    @staticmethod
    def _validate_settings(
        *,
        embedding_model: str,
        top_k: int,
        max_top_k: int,
        candidate_limit: int,
        min_score: float,
        reference_max_chars: int,
    ) -> None:
        if not embedding_model.strip():
            raise ValueError("embedding_model must not be empty")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        if (
            isinstance(max_top_k, bool)
            or not isinstance(max_top_k, int)
            or not 1 <= max_top_k <= 5
        ):
            raise ValueError("max_top_k must be between 1 and 5")
        if top_k > max_top_k:
            raise ValueError("top_k must not exceed max_top_k")
        if (
            isinstance(candidate_limit, bool)
            or not isinstance(candidate_limit, int)
            or candidate_limit < 1
        ):
            raise ValueError("candidate_limit must be a positive integer")
        if not isinstance(min_score, (int, float)) or isinstance(min_score, bool):
            raise ValueError("min_score must be numeric")
        if not math.isfinite(float(min_score)) or not -1.0 <= min_score <= 1.0:
            raise ValueError("min_score must be between -1 and 1")
        if (
            isinstance(reference_max_chars, bool)
            or not isinstance(reference_max_chars, int)
            or reference_max_chars < 1
        ):
            raise ValueError("reference_max_chars must be a positive integer")

    def retrieve(
        self,
        request: TripRequest,
        *,
        exclude_session_id: str | None = None,
        selected_attractions: Sequence[str] = (),
    ) -> RagContext:
        if not self._enabled:
            return RagContext(attempted=False, used=False, reason="disabled")

        started = self._safe_monotonic()
        try:
            query_text = self._text_builder.build_query(
                request,
                selected_attractions=selected_attractions,
            )
            city = _city(request.city)
            transportation = _transport(request.transportation)

            try:
                raw_vector = self._embedding_client.embed(query_text)
                vector = self._validated_vector(raw_vector)
            except Exception as error:
                return self._empty(
                    "embedding_unavailable",
                    started=started,
                    error=error,
                )

            merged: dict[str, VectorHit] = {}
            saw_below_threshold = False
            try:
                for stage in _STAGES:
                    stage_outcome = "empty"
                    try:
                        hits = self._vector_index.query(
                            vector,
                            city=city,
                            travel_days=request.travel_days,
                            transportation=transportation,
                            stage=stage,
                            limit=self._candidate_limit,
                            min_score=self._min_score,
                            exclude_share_ids=tuple(merged),
                        )
                        for raw_hit in hits:
                            normalized = self._normalized_hit(raw_hit, stage)
                            if normalized is None:
                                continue
                            if normalized.vector_score < self._min_score:
                                saw_below_threshold = True
                                continue
                            stage_outcome = "candidate"
                            self._merge_hit(merged, normalized)
                    except Exception:
                        stage_outcome = "failure"
                        raise
                    finally:
                        self._record_retrieval_stage(stage, stage_outcome)
            except Exception as error:
                return self._empty(
                    "qdrant_unavailable",
                    started=started,
                    error=error,
                )

            if not merged:
                reason = "below_threshold" if saw_below_threshold else "no_same_city_candidate"
                return self._empty(reason, started=started)

            identities = [
                IndexedIdentity(
                    share_id=hit.share_id,
                    index_version=hit.index_version,
                    content_hash=hit.content_hash,
                )
                for hit in merged.values()
            ]
            try:
                records = self._store.bulk_get_ready(
                    identities,
                    exclude_session_id=exclude_session_id,
                )
            except Exception as error:
                return self._empty(
                    "unexpected_error",
                    started=started,
                    error=error,
                )

            checked = self._application_recheck(records, merged, city)
            candidate_count = len(checked)
            if not checked:
                return self._empty("mysql_recheck_empty", started=started)

            now = self._clock()
            max_like_count = max(record.like_count for record, _ in checked)
            ranked: list[_RankedCandidate] = []
            for record, matched_hit in checked:
                try:
                    age_days = (now - record.published_at).total_seconds() / 86400.0
                    final_score = calculate_rerank_score(
                        vector_score=matched_hit.vector_score,
                        quality_score=record.quality_score,
                        age_days=age_days,
                        like_count=record.like_count,
                        max_like_count=max_like_count,
                    )
                    reference = self._build_reference(record, matched_hit, final_score)
                except Exception:
                    continue
                ranked.append(
                    _RankedCandidate(
                        record=record,
                        hit=matched_hit,
                        final_score=final_score,
                        reference=reference,
                    )
                )

            if not ranked:
                return self._empty(
                    "invalid_reference",
                    started=started,
                    candidate_count=candidate_count,
                )

            ranked = self._sort_candidates(ranked)[: self._top_k]
            while ranked and self._prompt_length(ranked) > self._reference_max_chars:
                ranked.pop()
            if not ranked:
                return self._empty(
                    "invalid_reference",
                    started=started,
                    candidate_count=candidate_count,
                )

            broadest_stage = max(
                (candidate.hit.filter_stage for candidate in ranked),
                key=_STAGE_RANK.__getitem__,
            )
            return self._context(
                attempted=True,
                used=True,
                reason="hit",
                filter_stage=broadest_stage,
                candidate_count=candidate_count,
                references=[candidate.reference for candidate in ranked],
                started=started,
            )
        except Exception as error:
            return self._empty("unexpected_error", started=started, error=error)

    @staticmethod
    def _validated_vector(raw_vector: object) -> list[float]:
        if isinstance(raw_vector, (str, bytes)):
            raise ValueError("embedding vector must be numeric")
        try:
            values = list(raw_vector)  # type: ignore[arg-type]
        except TypeError:
            raise ValueError("embedding vector must be a sequence") from None
        if len(values) != EMBEDDING_DIMENSION:
            raise ValueError("embedding vector dimension does not match configuration")
        vector: list[float] = []
        for value in values:
            if isinstance(value, bool):
                raise ValueError("embedding vector must be numeric")
            try:
                normalized = float(value)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("embedding vector must be numeric") from None
            if not math.isfinite(normalized):
                raise ValueError("embedding vector must contain finite values")
            vector.append(normalized)
        return vector

    def _safe_monotonic(self) -> float:
        try:
            value = float(self._monotonic())
            if math.isfinite(value):
                return value
        except Exception:
            pass
        try:
            value = float(time.monotonic())
            if math.isfinite(value):
                return value
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _normalized_hit(
        raw_hit: object,
        stage: RetrievalFilterStage,
    ) -> VectorHit | None:
        try:
            score = raw_hit.vector_score  # type: ignore[attr-defined]
            if isinstance(score, bool):
                return None
            score = float(score)
            if not math.isfinite(score):
                return None
            return VectorHit(
                share_id=str(raw_hit.share_id),  # type: ignore[attr-defined]
                index_version=int(raw_hit.index_version),  # type: ignore[attr-defined]
                content_hash=str(raw_hit.content_hash),  # type: ignore[attr-defined]
                vector_score=score,
                filter_stage=stage,
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _merge_hit(merged: dict[str, VectorHit], incoming: VectorHit) -> None:
        previous = merged.get(incoming.share_id)
        if previous is None:
            merged[incoming.share_id] = incoming
            return
        earliest_stage = min(
            (previous.filter_stage, incoming.filter_stage),
            key=_STAGE_RANK.__getitem__,
        )
        strongest = incoming if incoming.vector_score > previous.vector_score else previous
        merged[incoming.share_id] = VectorHit(
            share_id=strongest.share_id,
            index_version=strongest.index_version,
            content_hash=strongest.content_hash,
            vector_score=strongest.vector_score,
            filter_stage=earliest_stage,
        )

    @staticmethod
    def _application_recheck(
        records: Sequence[SharedGuideRecord],
        merged: dict[str, VectorHit],
        city: str,
    ) -> list[tuple[SharedGuideRecord, VectorHit]]:
        checked: list[tuple[SharedGuideRecord, VectorHit]] = []
        seen: set[str] = set()
        for record in records:
            matched_hit = merged.get(record.share_id)
            if matched_hit is None or record.share_id in seen:
                continue
            if record.publication_status is not PublicationStatus.PUBLIC:
                continue
            if record.index_status is not ShareIndexStatus.READY:
                continue
            if record.index_version != matched_hit.index_version:
                continue
            if record.content_hash != matched_hit.content_hash:
                continue
            if _city(record.city_normalized or record.city) != city:
                continue
            if _city(record.city) != city or _city(record.snapshot.request.city) != city:
                continue
            seen.add(record.share_id)
            checked.append((record, matched_hit))
        return checked

    @staticmethod
    def _build_reference(
        record: SharedGuideRecord,
        hit: VectorHit,
        final_score: float,
    ) -> RagReference:
        request = record.snapshot.request
        plan = record.snapshot.trip_plan
        attraction_names: list[str] = []
        daily_summaries: list[str] = []
        for index, day in enumerate(plan.days[:30], start=1):
            names = _unique([item.name for item in day.attractions], limit=100)
            for name in names:
                if name not in attraction_names and len(attraction_names) < 60:
                    attraction_names.append(name)
            prefix = f"第{index}天："
            summary = _clean(day.description, 500 - len(prefix))
            if not summary:
                summary = EmbeddingTextBuilder._fallback_summary(
                    names,
                    500 - len(prefix),
                )
            daily_summaries.append(f"{prefix}{summary}")
        return RagReference(
            share_id=record.share_id,
            title=_clean(record.title, 200),
            city=_city(request.city or record.city),
            travel_days=request.travel_days,
            transportation=_transport(request.transportation),
            preferences=_unique(request.preferences, limit=64)[:20],
            attraction_names=attraction_names,
            daily_summaries=daily_summaries,
            overall_suggestions=_clean(plan.overall_suggestions, 1200),
            vector_score=hit.vector_score,
            final_score=final_score,
        )

    @staticmethod
    def _sort_candidates(candidates: list[_RankedCandidate]) -> list[_RankedCandidate]:
        ranked = sorted(candidates, key=lambda item: item.record.share_id)
        ranked.sort(key=lambda item: item.record.published_at, reverse=True)
        ranked.sort(key=lambda item: item.hit.vector_score, reverse=True)
        ranked.sort(key=lambda item: item.final_score, reverse=True)
        return ranked

    @staticmethod
    def _prompt_length(candidates: Sequence[_RankedCandidate]) -> int:
        payload = [candidate.reference.prompt_payload() for candidate in candidates]
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _empty(
        self,
        reason: str,
        *,
        started: float,
        candidate_count: int = 0,
        error: BaseException | None = None,
    ) -> RagContext:
        context = self._context(
            attempted=True,
            used=False,
            reason=reason,
            candidate_count=candidate_count,
            references=[],
            started=started,
        )
        if error is not None:
            logger.warning(
                "rag_retrieval error_class=%s duration_ms=%d",
                type(error).__name__,
                context.duration_ms,
            )
        return context

    def _record_retrieval_stage(
        self,
        stage: RetrievalFilterStage,
        outcome: str,
    ) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics.record_retrieval_stage(stage=stage, outcome=outcome)
        except Exception:
            logger.debug("failed to record rag retrieval stage metrics", exc_info=True)

    def _context(
        self,
        *,
        attempted: bool,
        used: bool,
        reason: str,
        candidate_count: int,
        references: list[RagReference],
        started: float,
        filter_stage: RetrievalFilterStage | None = None,
    ) -> RagContext:
        duration_ms = int(max(0.0, (self._safe_monotonic() - started) * 1000.0))
        context = RagContext(
            attempted=attempted,
            used=used,
            reason=reason,
            filter_stage=filter_stage,
            candidate_count=candidate_count,
            references=references,
            embedding_model=self._embedding_model,
            template_version=self._template_version,
            duration_ms=duration_ms,
        )
        if self._metrics is not None:
            try:
                self._metrics.record_retrieval(
                    outcome=reason,
                    duration_seconds=duration_ms / 1000.0,
                    stage=filter_stage or "unknown",
                    candidate_count=candidate_count,
                )
            except Exception:
                logger.debug("failed to record rag retrieval metrics", exc_info=True)
        return context
