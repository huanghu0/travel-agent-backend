"""HTTP-facing shared-guide schemas with explicit public projections."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ShareTitleRequest(BaseModel):
    """The only client-controlled field for sharing or updating a guide."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)


class SharedGuideListItemResponse(BaseModel):
    share_id: str
    title: str
    author_username: str
    city: str
    travel_days: int
    transportation: str
    preferences: list[str] = Field(default_factory=list)
    cover_image_url: str | None = None
    quality_score: float | None = None
    like_count: int = Field(ge=0)
    published_at: datetime
    liked_by_me: bool = False


class SharedGuideDetailResponse(SharedGuideListItemResponse):
    snapshot: dict[str, Any]


class SharedGuidePageResponse(BaseModel):
    items: list[SharedGuideListItemResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class OwnedSharedGuideListItemResponse(SharedGuideListItemResponse):
    publication_status: str
    index_status: str
    last_index_error: str | None = None


class OwnedSharedGuidePageResponse(BaseModel):
    items: list[OwnedSharedGuideListItemResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class LikeMutationResponse(BaseModel):
    liked: bool
    like_count: int = Field(ge=0)
