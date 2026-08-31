"""Provider-neutral protocols reserved for later RAG integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Protocol, Sequence

from app.schemas.trip_schema import TripRequest

from .models import IndexedIdentity, RagContext, RetrievalFilterStage, VectorHit

if TYPE_CHECKING:
    from app.sharing.models import SharedGuideRecord


class EmbeddingClient(Protocol):
    model: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...


class SharedGuideVectorIndex(Protocol):
    def ensure_collection(self) -> None: ...

    def upsert(
        self,
        share_id: str,
        vector: Sequence[float],
        *,
        payload: Mapping[str, object],
    ) -> None: ...

    def delete(self, share_id: str, *, index_version: int) -> None: ...

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
    ) -> list[VectorHit]: ...


class SharedGuideIndex(SharedGuideVectorIndex, Protocol):
    """Compatibility name for callers using the original index protocol."""


class SharedGuideReadyStore(Protocol):
    def bulk_get_ready(
        self,
        identities: Sequence[IndexedIdentity],
        exclude_session_id: str | None = None,
    ) -> list["SharedGuideRecord"]: ...


class RagRetriever(Protocol):
    def retrieve(
        self,
        request: TripRequest,
        *,
        exclude_session_id: str | None = None,
        selected_attractions: Sequence[str] = (),
    ) -> RagContext: ...
