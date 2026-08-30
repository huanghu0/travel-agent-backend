"""Synchronous HTTP endpoints for the shared-guide square."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth.models import User
from app.sharing.exceptions import (
    InvalidShareCursorError,
    SharedGuideConflictError,
    SharedGuideForbiddenError,
    SharedGuideNotFoundError,
    SharedGuideUnavailableError,
)
from app.sharing.models import (
    LikeMutation,
    OwnedSharedGuideListItem,
    OwnedSharedGuidePage,
    SharedGuideListItem,
    SharedGuidePage,
    SharedGuidePublicDetail,
    SharedGuideRecord,
)
from app.sharing.schemas import (
    LikeMutationResponse,
    OwnedSharedGuideListItemResponse,
    OwnedSharedGuidePageResponse,
    SharedGuideDetailResponse,
    SharedGuideListItemResponse,
    SharedGuidePageResponse,
    ShareTitleRequest,
)
from app.sharing.service import SharedGuideService


_T = TypeVar("_T")
_PRIVATE_SNAPSHOT_KEYS = {
    "author_user_id",
    "content_hash",
    "embedding_dimension",
    "embedding_model",
    "free_text_input",
    "index_status",
    "index_version",
    "indexed_at",
    "last_index_error",
    "publication_status",
    "retrieval_template_version",
    "retrieval_text",
    "source_session_id",
    "source_version_id",
    "source_version_number",
}
_SAFE_ERROR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


def build_shared_guide_router(
    shared_guide_service: SharedGuideService | None,
    current_user_dependency: Callable[..., User],
    optional_current_user_dependency: Callable[..., User | None],
    *,
    default_list_limit: int = 20,
    max_list_limit: int = 50,
) -> APIRouter:
    """Build the API router without coupling it to application startup wiring."""

    if default_list_limit < 1 or default_list_limit > max_list_limit:
        raise ValueError("default_list_limit must be within the configured maximum")
    if max_list_limit < 1:
        raise ValueError("max_list_limit must be positive")

    router = APIRouter(prefix="/api", tags=["shared-guides"])

    @router.get("/shared-guides", response_model=SharedGuidePageResponse)
    def list_shared_guides(
        city: str | None = Query(default=None),
        travel_days: int | None = Query(default=None, ge=1, le=30),
        transportation: str | None = Query(default=None),
        sort: Literal["latest", "popular"] = Query(default="latest"),
        limit: int = Query(default=default_list_limit, ge=1, le=max_list_limit),
        cursor: str | None = Query(default=None),
        current_user: User | None = Depends(optional_current_user_dependency),
    ) -> SharedGuidePageResponse:
        page = _invoke(
            lambda: _service(shared_guide_service).list_public(
                city=city,
                travel_days=travel_days,
                transportation=transportation,
                sort=sort,
                limit=limit,
                cursor=cursor,
                viewer_user_id=current_user.user_id if current_user else None,
            )
        )
        return _public_page(page)

    @router.get("/shared-guides/{share_id}", response_model=SharedGuideDetailResponse)
    def get_shared_guide(
        share_id: str,
        current_user: User | None = Depends(optional_current_user_dependency),
    ) -> SharedGuideDetailResponse:
        detail = _invoke(
            lambda: _service(shared_guide_service).get_public(
                share_id,
                viewer_user_id=current_user.user_id if current_user else None,
            )
        )
        return _public_detail(detail)

    @router.post(
        "/trip/sessions/{session_id}/share",
        response_model=OwnedSharedGuideListItemResponse,
    )
    def share_session(
        session_id: str,
        payload: ShareTitleRequest,
        current_user: User = Depends(current_user_dependency),
    ) -> OwnedSharedGuideListItemResponse:
        record = _invoke(
            lambda: _service(shared_guide_service).share_session(
                session_id,
                current_user.user_id,
                title=payload.title,
            )
        )
        return _owned_record(record, current_user.username)

    @router.put(
        "/shared-guides/{share_id}",
        response_model=OwnedSharedGuideListItemResponse,
    )
    def update_shared_guide(
        share_id: str,
        payload: ShareTitleRequest,
        current_user: User = Depends(current_user_dependency),
    ) -> OwnedSharedGuideListItemResponse:
        record = _invoke(
            lambda: _service(shared_guide_service).update(
                share_id,
                current_user.user_id,
                title=payload.title,
            )
        )
        return _owned_record(record, current_user.username)

    @router.delete("/shared-guides/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
    def unpublish_shared_guide(
        share_id: str,
        current_user: User = Depends(current_user_dependency),
    ) -> Response:
        _invoke(
            lambda: _service(shared_guide_service).unpublish(
                share_id,
                current_user.user_id,
            )
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/users/me/shared-guides",
        response_model=OwnedSharedGuidePageResponse,
    )
    def list_my_shared_guides(
        city: str | None = Query(default=None),
        travel_days: int | None = Query(default=None, ge=1, le=30),
        transportation: str | None = Query(default=None),
        sort: Literal["latest", "popular"] = Query(default="latest"),
        limit: int = Query(default=default_list_limit, ge=1, le=max_list_limit),
        cursor: str | None = Query(default=None),
        current_user: User = Depends(current_user_dependency),
    ) -> OwnedSharedGuidePageResponse:
        page = _invoke(
            lambda: _service(shared_guide_service).list_owned(
                current_user.user_id,
                city=city,
                travel_days=travel_days,
                transportation=transportation,
                sort=sort,
                limit=limit,
                cursor=cursor,
            )
        )
        return _owned_page(page)

    @router.put(
        "/shared-guides/{share_id}/like",
        response_model=LikeMutationResponse,
    )
    def like_shared_guide(
        share_id: str,
        current_user: User = Depends(current_user_dependency),
    ) -> LikeMutationResponse:
        return _like_response(
            _invoke(
                lambda: _service(shared_guide_service).like(
                    share_id,
                    current_user.user_id,
                )
            )
        )

    @router.delete(
        "/shared-guides/{share_id}/like",
        response_model=LikeMutationResponse,
    )
    def unlike_shared_guide(
        share_id: str,
        current_user: User = Depends(current_user_dependency),
    ) -> LikeMutationResponse:
        return _like_response(
            _invoke(
                lambda: _service(shared_guide_service).unlike(
                    share_id,
                    current_user.user_id,
                )
            )
        )

    return router


def _service(service: SharedGuideService | None) -> SharedGuideService:
    if service is None:
        raise SharedGuideUnavailableError("shared guide service is unavailable")
    return service


def _invoke(callback: Callable[[], _T]) -> _T:
    try:
        return callback()
    except InvalidShareCursorError as exc:
        raise HTTPException(status_code=400, detail="无效的分享列表游标") from exc
    except SharedGuideForbiddenError as exc:
        raise HTTPException(status_code=403, detail="不允许执行该分享操作") from exc
    except SharedGuideNotFoundError as exc:
        raise HTTPException(status_code=404, detail="分享攻略不存在") from exc
    except SharedGuideConflictError as exc:
        raise HTTPException(status_code=409, detail="分享操作冲突") from exc
    except SharedGuideUnavailableError as exc:
        raise HTTPException(status_code=503, detail="分享服务暂不可用") from exc


def _public_page(page: SharedGuidePage) -> SharedGuidePageResponse:
    return SharedGuidePageResponse(
        items=[_public_item(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


def _public_item(item: SharedGuideListItem) -> SharedGuideListItemResponse:
    return SharedGuideListItemResponse(
        share_id=item.share_id,
        title=item.title,
        author_username=item.author_username,
        city=item.city,
        travel_days=item.travel_days,
        transportation=item.transportation,
        preferences=list(item.preferences),
        cover_image_url=item.cover_image_url,
        quality_score=item.quality_score,
        like_count=item.like_count,
        published_at=item.published_at,
        liked_by_me=item.liked_by_me,
    )


def _public_detail(detail: SharedGuidePublicDetail) -> SharedGuideDetailResponse:
    item = _public_item(detail).model_dump()
    item["cover_image_url"] = detail.cover_image_url or _cover_image_url(
        detail.snapshot
    )
    return SharedGuideDetailResponse(
        **item,
        snapshot=_redact_snapshot(detail.snapshot.model_dump()),
    )


def _owned_page(page: OwnedSharedGuidePage) -> OwnedSharedGuidePageResponse:
    return OwnedSharedGuidePageResponse(
        items=[_owned_item(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


def _owned_item(item: OwnedSharedGuideListItem) -> OwnedSharedGuideListItemResponse:
    return OwnedSharedGuideListItemResponse(
        **_public_item(item).model_dump(),
        publication_status=item.publication_status.value,
        index_status=item.index_status.value,
        last_index_error=_safe_index_error(item.last_index_error),
    )


def _owned_record(record: SharedGuideRecord, username: str) -> OwnedSharedGuideListItemResponse:
    if record.published_at is None:
        raise HTTPException(status_code=503, detail="分享服务暂不可用")
    return OwnedSharedGuideListItemResponse(
        share_id=record.share_id,
        title=record.title,
        author_username=username,
        city=record.city,
        travel_days=record.travel_days,
        transportation=record.transportation,
        preferences=list(record.preferences),
        cover_image_url=_cover_image_url(record.snapshot),
        quality_score=record.quality_score,
        like_count=record.like_count,
        published_at=record.published_at,
        liked_by_me=False,
        publication_status=record.publication_status.value,
        index_status=record.index_status.value,
        last_index_error=_safe_index_error(record.last_index_error),
    )


def _like_response(mutation: LikeMutation) -> LikeMutationResponse:
    return LikeMutationResponse(liked=mutation.liked, like_count=mutation.like_count)


def _cover_image_url(snapshot: Any) -> str | None:
    for day in snapshot.trip_plan.days:
        for attraction in day.attractions:
            if attraction.image_url:
                return attraction.image_url
            if attraction.photos:
                for photo in attraction.photos:
                    if photo:
                        return photo
    return None


def _redact_snapshot(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_snapshot(item)
            for key, item in value.items()
            if not _is_private_snapshot_key(key)
        }
    if isinstance(value, list):
        return [_redact_snapshot(item) for item in value]
    return value


def _is_private_snapshot_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in _PRIVATE_SNAPSHOT_KEYS
        or normalized == "poi_id"
        or normalized.endswith("_source_id")
        or normalized.startswith("rag_")
        or normalized.startswith("retrieval_")
        or normalized.endswith("_hash")
    )


def _safe_index_error(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.split(":", 1)[0].strip()
    return candidate if _SAFE_ERROR_NAME.fullmatch(candidate) else "indexing_failed"
