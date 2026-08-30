"""Domain records for immutable public trip-guide snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.trip_schema import TripPlan


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PublicationStatus(str, Enum):
    PUBLISHING = "PUBLISHING"
    PUBLIC = "PUBLIC"
    UNPUBLISHED = "UNPUBLISHED"


class ShareIndexStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    DELETE_PENDING = "DELETE_PENDING"
    DELETED = "DELETED"


class IndexOperation(str, Enum):
    UPSERT = "UPSERT"
    DELETE = "DELETE"


class IndexJobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SharedTripRequestSnapshot(BaseModel):
    city: str
    travel_days: int = Field(ge=1, le=30)
    transportation: str
    accommodation: str
    preferences: list[str] = Field(default_factory=list)


class SharedGuideSnapshot(BaseModel):
    request: SharedTripRequestSnapshot
    trip_plan: TripPlan


class SharePublishDraft(BaseModel):
    author_user_id: str
    source_session_id: str
    source_version_id: str
    source_version_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    city: str
    city_normalized: str
    travel_days: int = Field(ge=1, le=30)
    transportation: str
    accommodation: str
    preferences: list[str] = Field(default_factory=list)
    snapshot: SharedGuideSnapshot
    retrieval_text: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    quality_level: str
    quality_score: float | None = Field(default=None, ge=0, le=100)
    embedding_model: str = Field(min_length=1, max_length=128)
    embedding_dimension: int = Field(default=768, gt=0)
    retrieval_template_version: str = Field(min_length=1, max_length=64)


class SharedGuideRecord(SharePublishDraft):
    share_id: str
    publication_status: PublicationStatus = PublicationStatus.PUBLISHING
    index_status: ShareIndexStatus = ShareIndexStatus.PENDING
    index_version: int = Field(default=1, ge=1)
    like_count: int = Field(default=0, ge=0)
    last_index_error: str | None = None
    indexed_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("indexed_at", "published_at", "created_at", "updated_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value)):
            raise ValueError("timestamps must be timezone-aware UTC values")
        return value

    @model_validator(mode="after")
    def validate_public_ready_timestamps(self) -> "SharedGuideRecord":
        if self.index_status is ShareIndexStatus.READY and self.indexed_at is None:
            raise ValueError("READY records require indexed_at")
        if self.publication_status is PublicationStatus.PUBLIC:
            if self.index_status is not ShareIndexStatus.READY:
                raise ValueError("PUBLIC records require READY index status")
            if self.published_at is None:
                raise ValueError("PUBLIC records require published_at")
        if self.indexed_at and self.published_at and self.published_at > self.indexed_at:
            raise ValueError("published_at cannot follow indexed_at")
        return self


class SharedGuideListQuery(BaseModel):
    city_normalized: str | None = None
    travel_days: int | None = Field(default=None, ge=1, le=30)
    transportation: str | None = None
    sort: Literal["latest", "popular"] = "latest"
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = None


class SharedGuideListItem(BaseModel):
    share_id: str
    title: str
    author_username: str
    city: str
    travel_days: int
    transportation: str
    accommodation: str
    preferences: list[str] = Field(default_factory=list)
    quality_level: str
    quality_score: float | None = None
    like_count: int = Field(ge=0)
    published_at: datetime
    cover_image_url: str | None = None
    liked_by_me: bool = False


class SharedGuidePublicDetail(SharedGuideListItem):
    snapshot: SharedGuideSnapshot


class SharedGuidePage(BaseModel):
    items: list[SharedGuideListItem] = Field(default_factory=list)
    next_cursor: str | None = None


class OwnedSharedGuideListItem(SharedGuideListItem):
    publication_status: PublicationStatus
    index_status: ShareIndexStatus
    last_index_error: str | None = None


class OwnedSharedGuidePage(BaseModel):
    items: list[OwnedSharedGuideListItem] = Field(default_factory=list)
    next_cursor: str | None = None


class ShareIndexJob(BaseModel):
    job_id: str
    share_id: str
    operation: IndexOperation
    index_version: int = Field(ge=1)
    status: IndexJobStatus = IndexJobStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    lease_owner: str | None = Field(default=None, max_length=128)
    lease_expires_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ShareIndexIntent(BaseModel):
    record: SharedGuideRecord
    job: ShareIndexJob | None = None
    created: bool
    operation_required: bool


class LikeMutation(BaseModel):
    liked: bool
    like_count: int = Field(ge=0)
