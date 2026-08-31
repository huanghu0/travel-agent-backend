from __future__ import annotations

import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from qdrant_client import QdrantClient, models

from app.rag.qdrant_index import (
    QdrantSchemaMismatchError,
    QdrantSharedGuideIndex,
    RetrievalFilterStage,
    create_qdrant_client,
)


APPROVED_PAYLOAD = {
    "share_id": "6e9d4219-2994-4d30-88eb-2f3ce52b0f62",
    "city": "北京",
    "travel_days": 3,
    "transportation": "公共交通",
    "visibility": "PUBLIC",
    "quality_score": 88.5,
    "published_at": 1787673600,
    "index_version": 2,
    "content_hash": "a" * 64,
}


def collection_info(*, size: int = 768, distance: models.Distance = models.Distance.COSINE):
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors=models.VectorParams(size=size, distance=distance))
        )
    )


class AlreadyExistsError(RuntimeError):
    status_code = 409


class FakeQdrantClient:
    def __init__(self, *, exists: bool = False, info=None, hits=(), create_error=None):
        self.exists = exists
        self.info = info or collection_info()
        self.hits = list(hits)
        self.create_error = create_error
        self.create_collection_calls = []
        self.create_payload_index_calls = []
        self.upsert_calls = []
        self.delete_calls = []
        self.query_calls = []
        self.delete_collection_calls = []

    def collection_exists(self, collection_name):
        return self.exists

    def get_collection(self, collection_name):
        return self.info

    def create_collection(self, **kwargs):
        self.create_collection_calls.append(kwargs)
        if self.create_error:
            raise self.create_error
        self.exists = True

    def create_payload_index(self, **kwargs):
        self.create_payload_index_calls.append(kwargs)

    def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)

    def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        return SimpleNamespace(points=self.hits)

    def delete_collection(self, **kwargs):
        self.delete_collection_calls.append(kwargs)


class QdrantSharedGuideIndexTests(unittest.TestCase):
    collection = "shared_guide_embeddings_v1"

    def make_index(self, fake, *, dimension=768, **kwargs):
        return QdrantSharedGuideIndex(
            client=fake,
            collection=self.collection,
            dimension=dimension,
            **kwargs,
        )

    def test_client_factory_uses_public_sdk_constructor_with_optional_api_key(self):
        with patch("app.rag.qdrant_index.QdrantClient") as constructor:
            create_qdrant_client(
                url="http://qdrant.example",
                api_key="",
                timeout_seconds=5.5,
            )

        constructor.assert_called_once_with(
            url="http://qdrant.example",
            api_key=None,
            timeout=5.5,
        )

    def test_ensure_collection_creates_unnamed_cosine_collection_and_indexes(self):
        fake = FakeQdrantClient(exists=False)

        self.make_index(fake).ensure_collection()

        self.assertEqual(len(fake.create_collection_calls), 1)
        created = fake.create_collection_calls[0]
        self.assertEqual(created["collection_name"], self.collection)
        self.assertIsInstance(created["vectors_config"], models.VectorParams)
        self.assertEqual(created["vectors_config"].size, 768)
        self.assertEqual(created["vectors_config"].distance, models.Distance.COSINE)
        self.assertEqual(
            [(call["field_name"], call["field_schema"]) for call in fake.create_payload_index_calls],
            [
                ("city", models.PayloadSchemaType.KEYWORD),
                ("travel_days", models.PayloadSchemaType.INTEGER),
                ("transportation", models.PayloadSchemaType.KEYWORD),
                ("visibility", models.PayloadSchemaType.KEYWORD),
            ],
        )
        for call in fake.create_payload_index_calls:
            self.assertEqual(call["collection_name"], self.collection)
        self.assertEqual(fake.delete_collection_calls, [])

    def test_ensure_collection_accepts_matching_existing_collection(self):
        fake = FakeQdrantClient(exists=True)

        self.make_index(fake).ensure_collection()

        self.assertEqual(fake.create_collection_calls, [])
        self.assertEqual(len(fake.create_payload_index_calls), 4)
        self.assertEqual(fake.delete_collection_calls, [])

    def test_ensure_collection_rejects_mismatched_schema_without_recreation(self):
        for info in (
            collection_info(size=1536),
            collection_info(distance=models.Distance.DOT),
        ):
            with self.subTest(info=info):
                fake = FakeQdrantClient(exists=True, info=info)
                with self.assertRaises(QdrantSchemaMismatchError):
                    self.make_index(fake).ensure_collection()
                self.assertEqual(fake.create_collection_calls, [])
                self.assertEqual(fake.create_payload_index_calls, [])
                self.assertEqual(fake.delete_collection_calls, [])

    def test_ensure_collection_validates_schema_after_already_exists_create_race(self):
        fake = FakeQdrantClient(
            exists=False,
            info=collection_info(size=1536),
            create_error=AlreadyExistsError("collection already exists"),
        )

        with self.assertRaises(QdrantSchemaMismatchError):
            self.make_index(fake).ensure_collection()

        self.assertEqual(len(fake.create_collection_calls), 1)
        self.assertEqual(fake.create_payload_index_calls, [])
        self.assertEqual(fake.delete_collection_calls, [])

    def test_v1_constructor_rejects_non_768_dimension_before_collection_access(self):
        fake = FakeQdrantClient(exists=True, info=collection_info(size=1536))

        with self.assertRaises(ValueError):
            self.make_index(fake, dimension=1536)

        self.assertEqual(fake.create_collection_calls, [])
        self.assertEqual(fake.create_payload_index_calls, [])
        self.assertEqual(fake.delete_collection_calls, [])

    def test_upsert_rejects_missing_approved_payload_field_before_qdrant_call(self):
        fake = FakeQdrantClient()
        payload = APPROVED_PAYLOAD.copy()
        del payload["content_hash"]

        with self.assertRaises(ValueError):
            self.make_index(fake).upsert(APPROVED_PAYLOAD["share_id"], [0.25] * 768, payload=payload)

        self.assertEqual(fake.upsert_calls, [])

    def test_upsert_uses_uuid_wait_and_approved_payload_only(self):
        fake = FakeQdrantClient()
        payload = APPROVED_PAYLOAD | {"snapshot": {"private": True}, "author_user_id": "user-1"}

        self.make_index(fake).upsert(APPROVED_PAYLOAD["share_id"], [0.25] * 768, payload=payload)

        call = fake.upsert_calls[0]
        self.assertEqual(call["collection_name"], self.collection)
        self.assertTrue(call["wait"])
        point = call["points"][0]
        self.assertEqual(point.id, uuid.UUID(APPROVED_PAYLOAD["share_id"]))
        self.assertEqual(point.vector, [0.25] * 768)
        self.assertEqual(point.payload, APPROVED_PAYLOAD)

    def test_delete_is_version_filtered_idempotent_and_waits(self):
        fake = FakeQdrantClient()

        self.make_index(fake).delete(APPROVED_PAYLOAD["share_id"], index_version=2)
        self.make_index(fake).delete(APPROVED_PAYLOAD["share_id"], index_version=2)

        self.assertEqual(len(fake.delete_calls), 2)
        selector = fake.delete_calls[0]["points_selector"]
        self.assertIsInstance(selector, models.FilterSelector)
        self.assertTrue(fake.delete_calls[0]["wait"])
        conditions = selector.filter.must
        self.assertEqual(
            [(condition.key, condition.match.value) for condition in conditions],
            [("share_id", APPROVED_PAYLOAD["share_id"]), ("index_version", 2)],
        )

    def test_all_stages_preserve_city_and_public_and_map_stage_constraints(self):
        fake = FakeQdrantClient()
        index = self.make_index(fake)
        expected = {
            RetrievalFilterStage.EXACT_DAYS_TRANSPORT: [("travel_days", 3), ("transportation", "公共交通")],
            RetrievalFilterStage.EXACT_DAYS: [("travel_days", 3)],
            RetrievalFilterStage.DAYS_PLUS_MINUS_ONE: [("travel_days", (2, 4))],
            RetrievalFilterStage.SAME_CITY: [],
        }

        for stage, extra in expected.items():
            with self.subTest(stage=stage):
                index.query(
                    [0.1] * 768,
                    city="北京",
                    travel_days=3,
                    transportation="公共交通",
                    stage=stage,
                    limit=20,
                    min_score=0.55,
                )
                query_filter = fake.query_calls[-1]["query_filter"]
                condition_values = []
                for condition in query_filter.must:
                    if condition.match is not None:
                        condition_values.append((condition.key, condition.match.value))
                    elif condition.range is not None:
                        condition_values.append((condition.key, (condition.range.gte, condition.range.lte)))
                self.assertIn(("city", "北京"), condition_values)
                self.assertIn(("visibility", "PUBLIC"), condition_values)
                for constraint in extra:
                    self.assertIn(constraint, condition_values)
                self.assertEqual(fake.query_calls[-1]["collection_name"], self.collection)
                self.assertEqual(fake.query_calls[-1]["limit"], 20)
                self.assertEqual(fake.query_calls[-1]["score_threshold"], 0.55)
                self.assertTrue(fake.query_calls[-1]["with_payload"])

    def test_plus_minus_one_never_queries_day_zero(self):
        fake = FakeQdrantClient()

        self.make_index(fake).query(
            [0.1] * 768,
            city="北京",
            travel_days=1,
            transportation="步行",
            stage=RetrievalFilterStage.DAYS_PLUS_MINUS_ONE,
            limit=20,
            min_score=0.55,
        )

        range_condition = next(
            condition for condition in fake.query_calls[0]["query_filter"].must if condition.range is not None
        )
        self.assertEqual(range_condition.key, "travel_days")
        self.assertEqual((range_condition.range.gte, range_condition.range.lte), (1.0, 2.0))

    def test_excluded_share_ids_use_must_not_without_weakening_city_or_public(self):
        fake = FakeQdrantClient()

        self.make_index(fake).query(
            [0.1] * 768,
            city="北京",
            travel_days=3,
            transportation="公共交通",
            stage=RetrievalFilterStage.SAME_CITY,
            limit=20,
            min_score=0.55,
            exclude_share_ids=[APPROVED_PAYLOAD["share_id"]],
        )

        query_filter = fake.query_calls[0]["query_filter"]
        self.assertEqual([(item.key, item.match.value) for item in query_filter.must], [("city", "北京"), ("visibility", "PUBLIC")])
        self.assertEqual(query_filter.must_not[0].has_id, [uuid.UUID(APPROVED_PAYLOAD["share_id"])])

    def test_query_maps_valid_hits_and_ignores_malformed_payloads(self):
        fake = FakeQdrantClient(
            hits=[
                SimpleNamespace(score=0.91, payload=APPROVED_PAYLOAD),
                SimpleNamespace(score=0.80, payload=APPROVED_PAYLOAD | {"index_version": "wrong"}),
                SimpleNamespace(score=0.70, payload={"share_id": "not-a-uuid"}),
                SimpleNamespace(score="wrong", payload=APPROVED_PAYLOAD),
            ]
        )

        hits = self.make_index(fake).query(
            [0.1] * 768,
            city="北京",
            travel_days=3,
            transportation="公共交通",
            stage=RetrievalFilterStage.EXACT_DAYS_TRANSPORT,
            limit=20,
            min_score=0.55,
        )

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].share_id, APPROVED_PAYLOAD["share_id"])
        self.assertEqual(hits[0].index_version, 2)
        self.assertEqual(hits[0].content_hash, "a" * 64)
        self.assertEqual(hits[0].vector_score, 0.91)
        self.assertEqual(hits[0].filter_stage, RetrievalFilterStage.EXACT_DAYS_TRANSPORT)

    def test_query_ignores_non_hex_content_hash(self):
        fake = FakeQdrantClient(
            hits=[SimpleNamespace(score=0.91, payload=APPROVED_PAYLOAD | {"content_hash": "g" * 64})]
        )

        hits = self.make_index(fake).query(
            [0.1] * 768,
            city="北京",
            travel_days=3,
            transportation="公共交通",
            stage=RetrievalFilterStage.EXACT_DAYS_TRANSPORT,
            limit=20,
            min_score=0.55,
        )

        self.assertEqual(hits, [])


@unittest.skipUnless(os.getenv("RUN_QDRANT_INTEGRATION_TESTS") == "1", "set RUN_QDRANT_INTEGRATION_TESTS=1 to run Qdrant integration")
class QdrantIntegrationTests(unittest.TestCase):
    def test_isolated_collection_lifecycle(self):
        collection = f"task_7_qdrant_test_{uuid.uuid4().hex}"
        client = QdrantClient(url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
        index = QdrantSharedGuideIndex(client=client, collection=collection, dimension=768)
        try:
            index.ensure_collection()
            index.upsert(APPROVED_PAYLOAD["share_id"], [0.1] * 768, payload=APPROVED_PAYLOAD)
            hits = index.query(
                [0.1] * 768,
                city="北京",
                travel_days=3,
                transportation="公共交通",
                stage=RetrievalFilterStage.EXACT_DAYS_TRANSPORT,
                limit=10,
                min_score=0.0,
            )
            self.assertEqual([hit.share_id for hit in hits], [APPROVED_PAYLOAD["share_id"]])
            index.delete(APPROVED_PAYLOAD["share_id"], index_version=2)
            self.assertEqual(
                index.query(
                    [0.1] * 768,
                    city="北京",
                    travel_days=3,
                    transportation="公共交通",
                    stage=RetrievalFilterStage.EXACT_DAYS_TRANSPORT,
                    limit=10,
                    min_score=0.0,
                ),
                [],
            )
        finally:
            client.delete_collection(collection_name=collection)


if __name__ == "__main__":
    unittest.main()
