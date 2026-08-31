"""Runtime assembly tests for optional shared-guide RAG dependencies."""

from __future__ import annotations

import unittest
import threading
import time
from types import SimpleNamespace

from app.observability.rag_metrics import RagMetrics
from app.rag.qdrant_index import QdrantSchemaMismatchError
from app.rag.retrieval import NoOpRagRetriever, RagRetrievalService
from app.rag.runtime import RagRuntime
from app.sharing.service import SharedGuideService
from app.sharing.worker import ShareIndexWorker


class FakeSettings(SimpleNamespace):
    def validate_rag_settings(self) -> list[str]:
        return list(self.rag_errors)


def make_settings(**overrides) -> FakeSettings:
    values = {
        "SHARE_SQUARE_ENABLED": False,
        "RAG_ENABLED": False,
        "SHARE_INDEX_WORKER_ENABLED": True,
        "QDRANT_URL": "http://qdrant.example",
        "QDRANT_API_KEY": "qdrant-secret",
        "QDRANT_COLLECTION": "shared_guide_embeddings_v1",
        "QDRANT_TIMEOUT_SECONDS": 4.5,
        "DASHSCOPE_API_KEY": "dashscope-secret",
        "DASHSCOPE_BASE_URL": (
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        ),
        "EMBEDDING_MODEL": "qwen3.7-text-embedding",
        "EMBEDDING_DIMENSION": 768,
        "EMBEDDING_TIMEOUT_SECONDS": 10.0,
        "EMBEDDING_MAX_ATTEMPTS": 3,
        "RAG_TOP_K": 3,
        "RAG_MAX_TOP_K": 5,
        "RAG_CANDIDATE_LIMIT": 20,
        "RAG_MIN_SCORE": 0.55,
        "RAG_REFERENCE_MAX_CHARS": 6000,
        "SHARE_INDEX_WORKER_POLL_SECONDS": 1.0,
        "SHARE_INDEX_LEASE_SECONDS": 120.0,
        "SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS": 0.05,
        "SHARE_INDEX_MAX_ATTEMPTS": 5,
        "SHARE_INDEX_RETRY_BASE_SECONDS": 2.0,
        "SHARE_INDEX_RETRY_MAX_SECONDS": 300.0,
        "rag_errors": [],
    }
    values.update(overrides)
    return FakeSettings(**values)


class FakeEmbedding:
    model = "qwen3.7-text-embedding"
    dimension = 768

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1] * 768


class FakeQdrantClient:
    def __init__(self) -> None:
        self.health_calls: list[dict] = []
        self.health_error: Exception | None = None

    def get_collections(self, **kwargs):
        self.health_calls.append(kwargs)
        if self.health_error is not None:
            raise self.health_error
        return SimpleNamespace(collections=[])


class FakeVectorIndex:
    def __init__(self, client: FakeQdrantClient, *, ensure_error=None) -> None:
        self.client = client
        self.ensure_error = ensure_error
        self.ensure_calls = 0
        self.query_error: Exception | None = None

    def ensure_collection(self) -> None:
        self.ensure_calls += 1
        if self.ensure_error is not None:
            raise self.ensure_error

    def query(self, *args, **kwargs):
        if self.query_error is not None:
            raise self.query_error
        return []

    def upsert(self, *args, **kwargs) -> None:
        return None

    def delete(self, *args, **kwargs) -> None:
        return None


class FakeWorker:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class BlockingClaimStore:
    """Hold one real worker loop in a lease call until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def claim_next_index_job(self, *args, **kwargs):
        del args, kwargs
        with self._lock:
            self.calls += 1
        self.entered.set()
        self.release.wait(timeout=2.0)
        return None


class RuntimeFactories:
    def __init__(self, *, ensure_error=None) -> None:
        self.embedding_calls = 0
        self.embedding_kwargs: list[dict] = []
        self.qdrant_calls = 0
        self.index_calls = 0
        self.worker_calls = 0
        self.embedding = FakeEmbedding()
        self.client = FakeQdrantClient()
        self.index = FakeVectorIndex(self.client, ensure_error=ensure_error)
        self.worker: FakeWorker | None = None

    def embedding_factory(self, **kwargs):
        self.embedding_calls += 1
        self.embedding_kwargs.append(kwargs)
        return self.embedding

    def qdrant_factory(self, **kwargs):
        self.qdrant_calls += 1
        return self.client

    def index_factory(self, **kwargs):
        self.index_calls += 1
        self.asserted_client = kwargs["client"]
        return self.index

    def worker_factory(self, **kwargs):
        self.worker_calls += 1
        self.worker = FakeWorker(**kwargs)
        return self.worker

    def build(self, settings, store=object(), worker_factory=None):
        return RagRuntime.from_settings(
            settings=settings,
            shared_store=store,
            embedding_client_factory=self.embedding_factory,
            qdrant_client_factory=self.qdrant_factory,
            vector_index_factory=self.index_factory,
            worker_factory=worker_factory or self.worker_factory,
            metrics=RagMetrics(),
        )


class RagRuntimeTests(unittest.TestCase):
    def test_both_flags_off_create_no_clients_worker_or_external_calls(self):
        factories = RuntimeFactories()

        runtime = factories.build(make_settings())

        self.assertEqual("disabled", runtime.status)
        self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
        self.assertIsNone(runtime.embedding_client)
        self.assertIsNone(runtime.vector_index)
        self.assertIsNone(runtime.worker)
        self.assertEqual(0, factories.embedding_calls)
        self.assertEqual(0, factories.qdrant_calls)
        self.assertEqual(0, factories.index_calls)
        self.assertEqual(0, factories.worker_calls)

    def test_enabled_invalid_config_degrades_without_constructing_clients(self):
        factories = RuntimeFactories()
        settings = make_settings(
            RAG_ENABLED=True,
            DASHSCOPE_API_KEY="",
            rag_errors=["DASHSCOPE_API_KEY is required: secret-value"],
        )

        runtime = factories.build(settings)

        self.assertEqual("degraded", runtime.status)
        self.assertFalse(runtime.embedding_configured)
        self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
        self.assertEqual(0, factories.embedding_calls)
        self.assertNotIn("secret-value", str(runtime.health_snapshot()))

    def test_enabled_without_dashscope_base_url_degrades_before_clients(self):
        factories = RuntimeFactories()
        settings = make_settings(
            RAG_ENABLED=True,
            DASHSCOPE_BASE_URL="",
            rag_errors=["DASHSCOPE_BASE_URL is required: secret-value"],
        )

        runtime = factories.build(settings)

        self.assertEqual("degraded", runtime.status)
        self.assertFalse(runtime.embedding_configured)
        self.assertEqual(0, factories.embedding_calls)
        self.assertIn(
            "invalid_dashscope_base_url",
            runtime.health_snapshot()["reasons"],
        )
        self.assertNotIn("secret-value", str(runtime.health_snapshot()))

    def test_enabled_without_shared_store_degrades_before_external_clients(self):
        factories = RuntimeFactories()

        runtime = factories.build(make_settings(RAG_ENABLED=True), store=None)

        self.assertEqual("degraded", runtime.status)
        self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
        self.assertEqual(0, factories.embedding_calls)
        self.assertEqual(0, factories.qdrant_calls)

    def test_validation_is_scoped_to_the_enabled_feature(self):
        sharing_factories = RuntimeFactories()
        sharing_runtime = sharing_factories.build(
            make_settings(
                SHARE_SQUARE_ENABLED=True,
                RAG_ENABLED=False,
                RAG_MIN_SCORE=2.0,
                rag_errors=["RAG_MIN_SCORE must be between -1 and 1"],
            )
        )

        self.assertEqual("ready", sharing_runtime.status)
        self.assertIsInstance(sharing_runtime.retriever, NoOpRagRetriever)
        self.assertIsNotNone(sharing_runtime.worker)

        rag_factories = RuntimeFactories()
        rag_runtime = rag_factories.build(
            make_settings(
                SHARE_SQUARE_ENABLED=False,
                RAG_ENABLED=True,
                SHARE_INDEX_WORKER_ENABLED=False,
                SHARE_INDEX_WORKER_POLL_SECONDS=0,
                SHARE_INDEX_LEASE_SECONDS=0,
                SHARE_INDEX_MAX_ATTEMPTS=0,
                SHARE_INDEX_RETRY_BASE_SECONDS=0,
                SHARE_INDEX_RETRY_MAX_SECONDS=0,
                SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS=0,
                rag_errors=[
                    "SHARE_INDEX_LEASE_SECONDS must be positive",
                    "SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS must be positive",
                ],
            )
        )

        self.assertEqual("ready", rag_runtime.status)
        self.assertIsInstance(rag_runtime.retriever, RagRetrievalService)
        self.assertIsNone(rag_runtime.worker)

    def test_timed_out_worker_stop_can_restart_after_old_daemon_exits(self):
        factories = RuntimeFactories()
        store = BlockingClaimStore()
        runtime = factories.build(
            make_settings(
                SHARE_SQUARE_ENABLED=True,
                SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS=0.01,
            ),
            store=store,
            worker_factory=ShareIndexWorker,
        )

        try:
            runtime.start()
            self.assertTrue(store.entered.wait(timeout=1.0))

            runtime.stop()
            self.assertTrue(runtime.worker.running)

            runtime.start()
            self.assertTrue(runtime._started)
            store.release.set()

            deadline = time.monotonic() + 1.0
            while store.calls < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertGreaterEqual(store.calls, 2)
        finally:
            store.release.set()
            runtime.stop()

    def test_invalid_constructor_settings_degrade_before_any_external_client(self):
        cases = (
            {"RAG_ENABLED": True, "EMBEDDING_MODEL": ""},
            {"RAG_ENABLED": True, "EMBEDDING_MODEL": "text-embedding-v4"},
            {"RAG_ENABLED": True, "RAG_CANDIDATE_LIMIT": 0},
            {"SHARE_SQUARE_ENABLED": True, "SHARE_INDEX_WORKER_POLL_SECONDS": 0},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                factories = RuntimeFactories()
                runtime = factories.build(
                    make_settings(**overrides),
                    worker_factory=(
                        ShareIndexWorker
                        if "SHARE_INDEX_WORKER_POLL_SECONDS" in overrides
                        else None
                    ),
                )

                self.assertEqual("degraded", runtime.status)
                self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
                self.assertEqual(0, factories.embedding_calls)
                self.assertEqual(0, factories.qdrant_calls)
                self.assertEqual(0, factories.index_calls)

    def test_read_only_shared_service_accepts_invalid_write_settings(self):
        service = SharedGuideService(
            state_store=object(),
            trip_draft_service=object(),
            store=object(),
            text_builder=object(),
            embedding_client=None,
            vector_index=None,
            write_enabled=False,
            lease_seconds=0,
            max_attempts=0,
            retry_base_seconds=0,
            retry_max_seconds=0,
        )

        self.assertFalse(service.write_enabled)

    def test_embedding_configured_ignores_timeout_and_retry_validity(self):
        factories = RuntimeFactories()
        settings = make_settings(
            RAG_ENABLED=True,
            EMBEDDING_TIMEOUT_SECONDS=0,
            EMBEDDING_MAX_ATTEMPTS=0,
            rag_errors=["EMBEDDING_TIMEOUT_SECONDS must be positive"],
        )

        runtime = factories.build(settings)

        self.assertEqual("degraded", runtime.status)
        self.assertTrue(runtime.embedding_configured)

    def test_rag_flag_builds_real_retriever_and_ensures_collection_once(self):
        factories = RuntimeFactories()
        settings = make_settings(RAG_ENABLED=True)

        runtime = factories.build(settings)

        self.assertEqual("ready", runtime.status)
        self.assertIsInstance(runtime.retriever, RagRetrievalService)
        self.assertEqual(1, factories.embedding_calls)
        self.assertEqual(
            settings.DASHSCOPE_BASE_URL,
            factories.embedding_kwargs[0]["base_url"],
        )
        self.assertEqual(1, factories.qdrant_calls)
        self.assertEqual(1, factories.index_calls)
        self.assertEqual(1, factories.index.ensure_calls)
        self.assertIs(runtime.worker, factories.worker)

    def test_sharing_flag_is_independent_and_can_build_worker_without_rag(self):
        factories = RuntimeFactories()

        runtime = factories.build(make_settings(SHARE_SQUARE_ENABLED=True))

        self.assertEqual("ready", runtime.status)
        self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
        self.assertIsNotNone(runtime.embedding_client)
        self.assertIsNotNone(runtime.vector_index)
        self.assertIs(runtime.worker, factories.worker)
        self.assertEqual(1, factories.worker_calls)
        self.assertEqual(
            0.05,
            factories.worker.kwargs["shutdown_timeout_seconds"],
        )

    def test_worker_requires_ready_adapters_flag_store_and_either_feature(self):
        for overrides, store in (
            ({"SHARE_SQUARE_ENABLED": True, "SHARE_INDEX_WORKER_ENABLED": False}, object()),
            ({"RAG_ENABLED": True, "SHARE_INDEX_WORKER_ENABLED": False}, object()),
            ({}, object()),
            ({"SHARE_SQUARE_ENABLED": True}, None),
        ):
            with self.subTest(overrides=overrides, store=store):
                factories = RuntimeFactories()
                runtime = factories.build(make_settings(**overrides), store=store)
                self.assertIsNone(runtime.worker)
                self.assertEqual(0, factories.worker_calls)

    def test_schema_mismatch_degrades_optional_runtime_only(self):
        factories = RuntimeFactories(
            ensure_error=QdrantSchemaMismatchError("private collection details")
        )

        runtime = factories.build(make_settings(RAG_ENABLED=True))

        self.assertEqual("degraded", runtime.status)
        self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
        self.assertIsNone(runtime.worker)
        self.assertNotIn("private collection details", str(runtime.health_snapshot()))

    def test_health_probe_is_lightweight_never_embeds_and_uses_timeout(self):
        factories = RuntimeFactories()
        runtime = factories.build(make_settings(RAG_ENABLED=True))

        snapshot = runtime.health_snapshot(probe=True)

        self.assertEqual([{"timeout": 4.5}], factories.client.health_calls)
        self.assertEqual([], factories.embedding.calls)
        self.assertEqual("ready", snapshot["qdrant"])
        self.assertEqual("ready", snapshot["rag"])
        self.assertTrue(snapshot["embedding_configured"])

    def test_later_qdrant_failure_updates_health_without_becoming_fatal(self):
        factories = RuntimeFactories()
        runtime = factories.build(make_settings(RAG_ENABLED=True))
        factories.index.query_error = RuntimeError("private provider response")

        with self.assertRaises(RuntimeError):
            runtime.vector_index.query(
                [0.1] * 768,
                city="杭州",
                travel_days=2,
                transportation="公共交通",
                stage="same_city",
                limit=3,
                min_score=0.5,
            )

        snapshot = runtime.health_snapshot()
        self.assertEqual("degraded", snapshot["qdrant"])
        self.assertEqual("degraded", snapshot["rag"])
        self.assertNotIn("private provider response", str(snapshot))

    def test_runtime_lifecycle_starts_and_stops_worker_once(self):
        factories = RuntimeFactories()
        runtime = factories.build(make_settings(SHARE_SQUARE_ENABLED=True))

        runtime.start()
        runtime.start()
        runtime.stop()
        runtime.stop()
        runtime.start()
        runtime.stop()

        self.assertEqual(2, factories.worker.start_calls)
        self.assertEqual(2, factories.worker.stop_calls)


class RagMetricsTests(unittest.TestCase):
    def test_metric_labels_are_bounded_and_exclude_sensitive_dimensions(self):
        metrics = RagMetrics()
        labels = metrics.label_names()

        self.assertEqual(
            {
                "rag_outcomes": ("outcome",),
                "embedding_outcomes": ("outcome",),
                "qdrant_operations": ("operation", "outcome"),
                "share_publications": ("stage", "outcome"),
                "retrieval_stages": ("stage", "outcome"),
                "candidate_counts": ("stage",),
                "index_jobs": ("operation", "outcome"),
                "index_backlog": ("status",),
            },
            labels,
        )
        forbidden = {"share_id", "city", "user_id", "status_code", "exception"}
        self.assertTrue(forbidden.isdisjoint({item for group in labels.values() for item in group}))


if __name__ == "__main__":
    unittest.main()
