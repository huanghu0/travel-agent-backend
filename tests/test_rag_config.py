import importlib
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CONFIG_MODULE = "app.core.config"


class RagConfigTests(unittest.TestCase):
    def setUp(self):
        self.original_environment = os.environ.copy()

    def tearDown(self):
        with patch.dict(os.environ, self.original_environment, clear=True):
            importlib.reload(importlib.import_module(CONFIG_MODULE))

    def load_config(self, **values):
        with patch.dict(os.environ, values, clear=True):
            module = importlib.import_module(CONFIG_MODULE)
            return importlib.reload(module)

    def test_env_float_blank_and_unset_defaults_keep_float_type(self):
        config = self.load_config()

        with patch.dict(os.environ, {}, clear=True):
            unset = config._env_float("MISSING_OPTIONAL_FLOAT", 5)
        with patch.dict(os.environ, {"BLANK_OPTIONAL_FLOAT": "   "}, clear=True):
            blank = config._env_float("BLANK_OPTIONAL_FLOAT", 5)

        self.assertEqual(5.0, unset)
        self.assertIsInstance(unset, float)
        self.assertEqual(5.0, blank)
        self.assertIsInstance(blank, float)

    def build_runtime(self, config):
        from app.observability.rag_metrics import RagMetrics
        from app.rag.runtime import RagRuntime

        embedding_factory_calls = []
        qdrant_factory_calls = []
        index_factory_calls = []

        def embedding_factory(**kwargs):
            embedding_factory_calls.append(kwargs)
            return SimpleNamespace(model="qwen3.7-text-embedding", dimension=768)

        def qdrant_factory(**kwargs):
            qdrant_factory_calls.append(kwargs)
            return SimpleNamespace()

        class FakeIndex:
            def ensure_collection(self):
                return None

        def index_factory(**kwargs):
            index_factory_calls.append(kwargs)
            return FakeIndex()

        runtime = RagRuntime.from_settings(
            settings=config.settings,
            shared_store=object(),
            embedding_client_factory=embedding_factory,
            qdrant_client_factory=qdrant_factory,
            vector_index_factory=index_factory,
            metrics=RagMetrics(),
        )
        return runtime, embedding_factory_calls, qdrant_factory_calls, index_factory_calls

    def test_rag_defaults_are_disabled_and_use_pinned_limits(self):
        config = self.load_config()

        self.assertFalse(config.settings.SHARE_SQUARE_ENABLED)
        self.assertFalse(config.settings.RAG_ENABLED)
        self.assertEqual(
            config.settings.EMBEDDING_MODEL,
            "qwen3.7-text-embedding",
        )
        self.assertEqual(config.settings.EMBEDDING_DIMENSION, 768)
        self.assertEqual(config.settings.RAG_TOP_K, 3)
        self.assertEqual(config.settings.RAG_MAX_TOP_K, 5)

    def test_requirements_keep_pydantic_pinned(self):
        requirements = (
            Path(__file__).resolve().parents[1] / "requirements.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("pydantic==2.12.5", requirements.splitlines())

    def test_validate_rag_settings_rejects_invalid_limits_dimension_score_and_timeouts(
        self,
    ):
        config = self.load_config(
            RAG_TOP_K="6",
            RAG_MAX_TOP_K="5",
            EMBEDDING_DIMENSION="512",
            EMBEDDING_MODEL="text-embedding-v4",
            QDRANT_TIMEOUT_SECONDS="0",
            EMBEDDING_TIMEOUT_SECONDS="-1",
            RAG_MIN_SCORE="1.1",
        )

        errors = config.settings.validate_rag_settings()

        self.assertTrue(any("RAG_TOP_K" in error for error in errors))
        self.assertTrue(any("EMBEDDING_MODEL" in error for error in errors))
        self.assertTrue(any("EMBEDDING_DIMENSION" in error for error in errors))
        self.assertTrue(any("timeout" in error.lower() for error in errors))
        self.assertTrue(any("RAG_MIN_SCORE" in error for error in errors))

    def test_validate_rag_settings_requires_lease_margin(self):
        config = self.load_config(
            SHARE_INDEX_LEASE_SECONDS="60",
            EMBEDDING_TIMEOUT_SECONDS="10",
            EMBEDDING_MAX_ATTEMPTS="3",
            QDRANT_TIMEOUT_SECONDS="5",
        )

        errors = config.settings.validate_rag_settings()

        self.assertTrue(any("SHARE_INDEX_LEASE_SECONDS" in error for error in errors))

    def test_enabled_features_report_missing_external_configuration_without_import_error(
        self,
    ):
        for flag in ("SHARE_SQUARE_ENABLED", "RAG_ENABLED"):
            config = self.load_config(
                **{
                    flag: "true",
                    "QDRANT_URL": "",
                    "QDRANT_COLLECTION": "",
                    "DASHSCOPE_API_KEY": "",
                    "DASHSCOPE_BASE_URL": "",
                }
            )

            errors = config.settings.validate_rag_settings()

            self.assertTrue(any("QDRANT_URL" in error for error in errors))
            self.assertTrue(any("QDRANT_COLLECTION" in error for error in errors))
            self.assertTrue(any("DASHSCOPE_API_KEY" in error for error in errors))
            self.assertTrue(any("DASHSCOPE_BASE_URL" in error for error in errors))

    def test_inactive_malformed_rag_and_worker_values_do_not_abort_active_sharing_runtime(
        self,
    ):
        config = self.load_config(
            SHARE_SQUARE_ENABLED="true",
            RAG_ENABLED="false",
            SHARE_INDEX_WORKER_ENABLED="false",
            DASHSCOPE_API_KEY="offline-key",
            DASHSCOPE_BASE_URL=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            ),
            RAG_MIN_SCORE="not-a-number",
            RAG_CANDIDATE_LIMIT="not-an-integer",
            SHARE_INDEX_WORKER_POLL_SECONDS="not-a-number",
            SHARE_INDEX_SHUTDOWN_TIMEOUT_SECONDS="not-a-number",
        )

        runtime, embedding_calls, qdrant_calls, index_calls = self.build_runtime(config)

        self.assertEqual("ready", runtime.status)
        self.assertIsNone(runtime.worker)
        self.assertEqual(1, len(embedding_calls))
        self.assertEqual(1, len(qdrant_calls))
        self.assertEqual(1, len(index_calls))

    def test_active_malformed_rag_value_degrades_before_external_clients(self):
        config = self.load_config(
            RAG_ENABLED="true",
            DASHSCOPE_API_KEY="offline-key",
            DASHSCOPE_BASE_URL=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            ),
            RAG_MIN_SCORE="not-a-number",
        )

        runtime, embedding_calls, qdrant_calls, index_calls = self.build_runtime(config)

        from app.rag.retrieval import NoOpRagRetriever

        self.assertEqual("degraded", runtime.status)
        self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
        self.assertEqual([], embedding_calls)
        self.assertEqual([], qdrant_calls)
        self.assertEqual([], index_calls)
        self.assertIn("invalid_rag_min_score", runtime.health_snapshot()["reasons"])

    def test_active_malformed_worker_value_degrades_before_external_clients(self):
        config = self.load_config(
            SHARE_SQUARE_ENABLED="true",
            RAG_ENABLED="false",
            DASHSCOPE_API_KEY="offline-key",
            DASHSCOPE_BASE_URL=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            ),
            SHARE_INDEX_WORKER_POLL_SECONDS="not-a-number",
        )

        runtime, embedding_calls, qdrant_calls, index_calls = self.build_runtime(config)

        from app.rag.retrieval import NoOpRagRetriever

        self.assertEqual("degraded", runtime.status)
        self.assertIsInstance(runtime.retriever, NoOpRagRetriever)
        self.assertEqual([], embedding_calls)
        self.assertEqual([], qdrant_calls)
        self.assertEqual([], index_calls)
        self.assertIn(
            "invalid_share_index_worker_poll_seconds",
            runtime.health_snapshot()["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
