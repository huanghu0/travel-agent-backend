"""RAG value objects; no provider or persistence concerns belong here."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class BuiltRetrievalText(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    content_hash: str = Field(min_length=64, max_length=64)
    city_normalized: str
    transportation_normalized: str
    template_version: str = "retrieval_template_v1"


class RetrievalFilterStage(str, Enum):
    EXACT_DAYS_TRANSPORT = "exact_days_transport"
    EXACT_DAYS = "exact_days"
    DAYS_PLUS_MINUS_ONE = "days_plus_minus_one"
    SAME_CITY = "same_city"


class IndexedIdentity(BaseModel):
    share_id: str
    index_version: int
    content_hash: str


class VectorHit(IndexedIdentity):
    vector_score: float
    filter_stage: RetrievalFilterStage


class RagReference(BaseModel):
    """Approved public guide fields plus diagnostics kept outside prompts."""

    share_id: str
    title: str
    city: str
    travel_days: int = Field(ge=1, le=30)
    transportation: str
    preferences: list[str] = Field(default_factory=list)
    attraction_names: list[str] = Field(default_factory=list)
    daily_summaries: list[str] = Field(default_factory=list)
    overall_suggestions: str = ""
    vector_score: float
    final_score: float

    def prompt_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "city": self.city,
            "travel_days": self.travel_days,
            "transportation": self.transportation,
            "preferences": list(self.preferences),
            "attraction_names": list(self.attraction_names),
            "daily_summaries": list(self.daily_summaries),
            "overall_suggestions": self.overall_suggestions,
        }


class RagContext(BaseModel):
    attempted: bool = False
    used: bool = False
    reason: str = "disabled"
    filter_stage: RetrievalFilterStage | None = None
    candidate_count: int = 0
    references: list[RagReference] = Field(default_factory=list)
    embedding_model: str | None = None
    template_version: str | None = None
    duration_ms: int = 0

    def prompt_payload(self) -> list[dict[str, object]]:
        return [reference.prompt_payload() for reference in self.references]
