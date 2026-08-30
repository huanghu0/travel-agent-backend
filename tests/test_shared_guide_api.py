"""HTTP contract tests for the shared-guide square API."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import (
    build_current_user_dependency,
    build_optional_current_user_dependency,
)
from app.auth.models import User
from app.auth.security import InvalidAccessTokenError
from app.agents.planner_agent import PlannerAgent
from app.rag.models import VectorHit
from app.rag.retrieval import RagRetrievalService
from app.rag.text_builder import EmbeddingTextBuilder
from app.schemas.trip_schema import TripPlan, TripRequest
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
    PublicationStatus,
    ShareIndexStatus,
    SharedGuideListItem,
    SharedGuidePage,
    SharedGuidePublicDetail,
    SharedGuideRecord,
    SharedGuideSnapshot,
    SharedTripRequestSnapshot,
)
from app.sharing.router import build_shared_guide_router


NOW = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)
OWNER = User(user_id="owner-1", username="alice", created_at=NOW)
VIEWER = User(user_id="viewer-1", username="bob", created_at=NOW)


def make_snapshot(
    *,
    image_url: str | None = "https://images.example/hero.jpg",
    photo_urls: list[str] | None = None,
):
    return SharedGuideSnapshot.model_validate(
        {
            "request": {
                "city": "杭州",
                "travel_days": 2,
                "transportation": "公共交通",
                "accommodation": "酒店",
                "preferences": ["美食"],
            },
            "trip_plan": {
                "city": "杭州",
                "start_date": "2026-08-01",
                "end_date": "2026-08-02",
                "days": [
                    {
                        "date": "2026-08-01",
                        "day_index": 0,
                        "description": "西湖一日游",
                        "transportation": "公共交通",
                        "accommodation": "酒店",
                        "attractions": [
                            {
                                "name": "西湖",
                                "address": "西湖风景区",
                                "location": {"longitude": 120.15, "latitude": 30.25},
                                "visit_duration": 90,
                                "description": "湖景",
                                "image_url": image_url,
                                "photos": photo_urls
                                if photo_urls is not None
                                else ["https://images.example/fallback.jpg"],
                                "poi_id": "private-poi-id",
                            }
                        ],
                        "meals": [],
                    }
                ],
                "overall_suggestions": "错峰出行",
            },
        }
    )


def make_public_detail(
    *,
    liked_by_me: bool = False,
    image_url: str | None = "https://images.example/hero.jpg",
    photo_urls: list[str] | None = None,
    snapshot: SharedGuideSnapshot | None = None,
) -> SharedGuidePublicDetail:
    return SharedGuidePublicDetail(
        share_id="share-1",
        title="杭州周末攻略",
        author_username="alice",
        city="杭州",
        travel_days=2,
        transportation="公共交通",
        accommodation="酒店",
        preferences=["美食"],
        quality_level="excellent",
        quality_score=95.0,
        like_count=3,
        published_at=NOW,
        liked_by_me=liked_by_me,
        snapshot=(
            snapshot
            if snapshot is not None
            else make_snapshot(image_url=image_url, photo_urls=photo_urls)
        ),
    )


def make_record() -> SharedGuideRecord:
    return SharedGuideRecord(
        share_id="share-1",
        author_user_id=OWNER.user_id,
        source_session_id="private-session-id",
        source_version_id="private-version-id",
        source_version_number=7,
        title="杭州周末攻略",
        city="杭州",
        city_normalized="杭州",
        travel_days=2,
        transportation="公共交通",
        accommodation="酒店",
        preferences=["美食"],
        snapshot=make_snapshot(),
        retrieval_text="private retrieval text",
        content_hash="a" * 64,
        quality_level="excellent",
        quality_score=95.0,
        embedding_model="qwen3.7-text-embedding",
        embedding_dimension=768,
        retrieval_template_version="v1",
        publication_status=PublicationStatus.PUBLIC,
        index_status=ShareIndexStatus.READY,
        index_version=4,
        like_count=3,
        last_index_error="RuntimeError: provider-token=secret",
        indexed_at=NOW,
        published_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


class FakeAuthService:
    def resolve_token(self, token: str) -> User:
        if token == "owner-token":
            return OWNER
        if token == "viewer-token":
            return VIEWER
        raise InvalidAccessTokenError("invalid")


class FakeSharedGuideService:
    def __init__(self) -> None:
        self.record = make_record()
        self.calls: list[tuple[str, tuple, dict]] = []
        self.failure: Exception | None = None
        self.image_url: str | None = "https://images.example/hero.jpg"
        self.photo_urls: list[str] | None = None
        self.detail_snapshot: SharedGuideSnapshot | None = None

    def _call(self, name: str, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.failure is not None:
            raise self.failure

    def list_public(self, **kwargs):
        self._call("list_public", **kwargs)
        item = make_public_detail(
            liked_by_me=kwargs.get("viewer_user_id") == VIEWER.user_id,
            image_url=self.image_url,
            photo_urls=self.photo_urls,
        )
        return SharedGuidePage(items=[SharedGuideListItem.model_validate(item.model_dump())])

    def get_public(self, share_id: str, *, viewer_user_id: str | None = None):
        self._call("get_public", share_id, viewer_user_id=viewer_user_id)
        return make_public_detail(
            liked_by_me=viewer_user_id == VIEWER.user_id,
            image_url=self.image_url,
            photo_urls=self.photo_urls,
            snapshot=self.detail_snapshot,
        )

    def share_session(self, session_id: str, author_user_id: str, *, title: str | None = None):
        self._call("share_session", session_id, author_user_id, title=title)
        return self.record

    def update(self, share_id: str, author_user_id: str, *, title: str | None = None):
        self._call("update", share_id, author_user_id, title=title)
        return self.record

    def unpublish(self, share_id: str, author_user_id: str):
        self._call("unpublish", share_id, author_user_id)
        return self.record

    def list_owned(self, author_user_id: str, **kwargs):
        self._call("list_owned", author_user_id, **kwargs)
        item = OwnedSharedGuideListItem(
            **make_public_detail().model_dump(exclude={"snapshot"}),
            publication_status=PublicationStatus.PUBLIC,
            index_status=ShareIndexStatus.FAILED,
            last_index_error="RuntimeError: provider-token=secret",
        )
        return OwnedSharedGuidePage(items=[item])

    def like(self, share_id: str, user_id: str):
        self._call("like", share_id, user_id)
        return LikeMutation(liked=True, like_count=4)

    def unlike(self, share_id: str, user_id: str):
        self._call("unlike", share_id, user_id)
        return LikeMutation(liked=False, like_count=3)


class ReadOnlySharedGuideService(FakeSharedGuideService):
    """Model a real MySQL read store with optional indexing writes disabled."""

    @staticmethod
    def _write_unavailable():
        raise SharedGuideUnavailableError("shared guide writes are unavailable")

    def share_session(self, *args, **kwargs):
        self._write_unavailable()

    def update(self, *args, **kwargs):
        self._write_unavailable()

    def unpublish(self, *args, **kwargs):
        self._write_unavailable()

    def like(self, *args, **kwargs):
        self._write_unavailable()

    def unlike(self, *args, **kwargs):
        self._write_unavailable()


class SharedGuideApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeSharedGuideService()
        auth_service = FakeAuthService()
        required_user = build_current_user_dependency(auth_service)
        optional_user = build_optional_current_user_dependency(auth_service)
        app = FastAPI()
        app.include_router(
            build_shared_guide_router(
                self.service,
                required_user,
                optional_user,
                default_list_limit=2,
                max_list_limit=3,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

    @staticmethod
    def auth(token: str = "owner-token") -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_anonymous_public_reads_redact_private_data_and_derive_cover_image(self):
        listing = self.client.get("/api/shared-guides")
        detail = self.client.get("/api/shared-guides/share-1")

        self.assertEqual(200, listing.status_code)
        self.assertEqual(200, detail.status_code)
        self.assertFalse(listing.json()["items"][0]["liked_by_me"])
        self.assertEqual(
            "https://images.example/hero.jpg",
            detail.json()["cover_image_url"],
        )
        self.service.image_url = None
        fallback = self.client.get("/api/shared-guides/share-1")
        self.assertEqual(
            "https://images.example/fallback.jpg",
            fallback.json()["cover_image_url"],
        )
        self.service.photo_urls = []
        missing = self.client.get("/api/shared-guides/share-1")
        self.assertIsNone(missing.json()["cover_image_url"])
        payload = detail.json()
        serialized = str(payload)
        for forbidden in (
            "author_user_id",
            "source_session_id",
            "source_version_id",
            "private-poi-id",
            "poi_id",
            "free_text_input",
            "retrieval_text",
            "content_hash",
            "embedding_model",
            "index_version",
            "last_index_error",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_cover_image_uses_first_attraction_fallback_before_later_image(self):
        source = make_snapshot(
            image_url=None,
            photo_urls=["https://images.example/first-attraction.jpg"],
        )
        first = source.trip_plan.days[0].attractions[0]
        second = first.model_copy(
            update={
                "name": "灵隐寺",
                "image_url": "https://images.example/second-attraction.jpg",
                "photos": ["https://images.example/second-photo.jpg"],
            }
        )
        day = source.trip_plan.days[0].model_copy(
            update={"attractions": [first, second]}
        )
        self.service.detail_snapshot = source.model_copy(
            update={
                "trip_plan": source.trip_plan.model_copy(update={"days": [day]})
            }
        )

        response = self.client.get("/api/shared-guides/share-1")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "https://images.example/first-attraction.jpg",
            response.json()["cover_image_url"],
        )

    def test_valid_optional_token_marks_public_items_as_liked_and_bad_token_is_unauthorized(self):
        liked = self.client.get("/api/shared-guides", headers=self.auth("viewer-token"))
        bad = self.client.get("/api/shared-guides", headers=self.auth("bad-token"))

        self.assertEqual(200, liked.status_code)
        self.assertTrue(liked.json()["items"][0]["liked_by_me"])
        self.assertEqual(401, bad.status_code)

    def test_writes_require_auth_and_accept_only_title(self):
        unauthenticated = self.client.post("/api/trip/sessions/session-1/share", json={})
        rejected = self.client.post(
            "/api/trip/sessions/session-1/share",
            headers=self.auth(),
            json={
                "title": "公开标题",
                "author_user_id": "attacker",
                "publication_status": "PUBLIC",
                "like_count": 999,
                "snapshot": {"private": "input"},
            },
        )
        accepted = self.client.post(
            "/api/trip/sessions/session-1/share",
            headers=self.auth(),
            json={"title": "公开标题"},
        )

        self.assertEqual(401, unauthenticated.status_code)
        self.assertEqual(
            401,
            self.client.put("/api/shared-guides/share-1/like").status_code,
        )
        self.assertEqual(422, rejected.status_code)
        self.assertEqual(200, accepted.status_code)
        name, args, kwargs = self.service.calls[-1]
        self.assertEqual("share_session", name)
        self.assertEqual(("session-1", OWNER.user_id), args)
        self.assertEqual("公开标题", kwargs["title"])

    def test_query_bounds_and_sort_are_enforced_before_service_calls(self):
        cases = (
            "/api/shared-guides?travel_days=0",
            "/api/shared-guides?travel_days=31",
            "/api/shared-guides?limit=0",
            "/api/shared-guides?limit=4",
            "/api/shared-guides?sort=unknown",
        )
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(422, self.client.get(url).status_code)
        popular = self.client.get("/api/shared-guides?sort=popular&limit=3")
        self.assertEqual(200, popular.status_code)
        self.assertEqual("popular", self.service.calls[-1][2]["sort"])
        self.assertEqual(3, self.service.calls[-1][2]["limit"])

    def test_domain_errors_map_to_safe_http_contracts(self):
        cases = (
            ("/api/shared-guides?cursor=not-a-cursor", None, InvalidShareCursorError("bad"), 400),
            ("/api/shared-guides/hidden", None, SharedGuideNotFoundError("hidden"), 404),
            ("/api/shared-guides/share-1/like", self.auth("viewer-token"), SharedGuideForbiddenError("self"), 403),
            ("/api/shared-guides/share-1", self.auth(), SharedGuideNotFoundError("cross-owner"), 404),
            ("/api/trip/sessions/session-1/share", self.auth(), SharedGuideUnavailableError("provider secret"), 503),
            ("/api/shared-guides/share-1", self.auth(), SharedGuideConflictError("race"), 409),
        )
        methods = ("get", "get", "put", "put", "post", "put")
        for (url, headers, error, expected), method in zip(cases, methods):
            with self.subTest(url=url, error=type(error).__name__):
                self.service.failure = error
                request = getattr(self.client, method)
                response = (
                    request(url, headers=headers)
                    if method == "get"
                    else request(url, headers=headers, json={})
                )
                self.assertEqual(expected, response.status_code)
                self.assertNotIn("provider secret", response.text)
        self.service.failure = None

    def test_unpublish_is_empty_204_and_owned_projection_sanitizes_index_error(self):
        deleted = self.client.delete("/api/shared-guides/share-1", headers=self.auth())
        owned = self.client.get("/api/users/me/shared-guides", headers=self.auth())

        self.assertEqual(204, deleted.status_code)
        self.assertEqual(b"", deleted.content)
        self.assertEqual(200, owned.status_code)
        item = owned.json()["items"][0]
        self.assertEqual("PUBLIC", item["publication_status"])
        self.assertEqual("FAILED", item["index_status"])
        self.assertNotIn("provider-token", item["last_index_error"])

    def test_public_reads_remain_available_while_every_write_returns_503(self):
        auth_service = FakeAuthService()
        app = FastAPI()
        app.include_router(
            build_shared_guide_router(
                ReadOnlySharedGuideService(),
                build_current_user_dependency(auth_service),
                build_optional_current_user_dependency(auth_service),
            )
        )

        with TestClient(app) as client:
            self.assertEqual(200, client.get("/api/shared-guides").status_code)
            self.assertEqual(
                200,
                client.get("/api/shared-guides/share-1").status_code,
            )
            writes = (
                client.post(
                    "/api/trip/sessions/session-1/share",
                    headers=self.auth(),
                    json={},
                ),
                client.put(
                    "/api/shared-guides/share-1",
                    headers=self.auth(),
                    json={},
                ),
                client.delete(
                    "/api/shared-guides/share-1",
                    headers=self.auth(),
                ),
                client.put(
                    "/api/shared-guides/share-1/like",
                    headers=self.auth(),
                ),
                client.delete(
                    "/api/shared-guides/share-1/like",
                    headers=self.auth(),
                ),
            )

        self.assertEqual([503] * len(writes), [response.status_code for response in writes])

    def test_fake_end_to_end_share_read_like_retrieval_and_planner_context(self):
        shared = self.client.post(
            "/api/trip/sessions/session-1/share",
            headers=self.auth(),
            json={"title": "杭州周末攻略"},
        )
        anonymous = self.client.get("/api/shared-guides/share-1")
        liked = self.client.put(
            "/api/shared-guides/share-1/like",
            headers=self.auth("viewer-token"),
        )

        self.assertEqual(200, shared.status_code)
        self.assertEqual(200, anonymous.status_code)
        self.assertEqual(200, liked.status_code)
        self.assertEqual({"liked": True, "like_count": 4}, liked.json())

        record = self.service.record

        class Embedding:
            def embed(self, text):
                self.text = text
                return [0.01] * 768

        class Index:
            def query(self, vector, **kwargs):
                return [
                    VectorHit(
                        share_id=record.share_id,
                        index_version=record.index_version,
                        content_hash=record.content_hash,
                        vector_score=0.92,
                        filter_stage=kwargs["stage"],
                    )
                ]

        class Store:
            def bulk_get_ready(self, identities, exclude_session_id=None):
                self.identities = identities
                self.exclude_session_id = exclude_session_id
                return [record]

        request = TripRequest(
            city="杭州市",
            start_date="2026-09-01",
            end_date="2026-09-02",
            travel_days=2,
            transportation="地铁",
            accommodation="酒店",
            preferences=["美食"],
        )
        context = RagRetrievalService(
            embedding_client=Embedding(),
            vector_index=Index(),
            store=Store(),
            text_builder=EmbeddingTextBuilder(),
            enabled=True,
            embedding_model="qwen3.7-text-embedding",
        ).retrieve(request, exclude_session_id="different-session")

        class PlannerLLM:
            def invoke(self, instructions, input_text, response_model=None):
                self.instructions = instructions
                self.input_text = input_text
                self.response_model = response_model
                return record.snapshot.trip_plan.model_dump_json()

        llm = PlannerLLM()
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.prompt = "locked planner constraints"
        planner.llm = llm
        generated = TripPlan.model_validate(
            planner.generate_plan(request, {}, {}, {}, rag_context=context)
        )

        self.assertTrue(context.used)
        self.assertEqual(["杭州周末攻略"], [item.title for item in context.references])
        self.assertEqual("杭州", generated.city)
        self.assertIs(llm.response_model, TripPlan)
        self.assertIn("BEGIN_UNTRUSTED_SHARED_GUIDE_REFERENCES", llm.input_text)
        prompt_payload = llm.input_text.split(
            "BEGIN_UNTRUSTED_SHARED_GUIDE_REFERENCES", 1
        )[1].split("END_UNTRUSTED_SHARED_GUIDE_REFERENCES", 1)[0]
        serialized = json.dumps(json.loads(prompt_payload.strip()), ensure_ascii=False)
        for private_value in (
            record.author_user_id,
            record.source_session_id,
            record.retrieval_text,
            record.content_hash,
        ):
            self.assertNotIn(private_value, serialized)


if __name__ == "__main__":
    unittest.main()
