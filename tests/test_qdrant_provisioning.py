from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ensure_shared_guide_collection as provisioning


class FakeIndex:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.ensure_calls = 0

    def ensure_collection(self) -> None:
        self.ensure_calls += 1


def make_settings(**overrides):
    values = {
        "QDRANT_URL": "http://qdrant:6333",
        "QDRANT_API_KEY": "",
        "QDRANT_TIMEOUT_SECONDS": 5,
        "QDRANT_COLLECTION": "shared_guide_embeddings_v1",
        "EMBEDDING_DIMENSION": 768,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class QdrantProvisioningTests(unittest.TestCase):
    def test_provisioning_wires_v1_index_and_calls_ensure_once(self) -> None:
        client = object()
        observed = {}

        def client_factory(**kwargs):
            observed["client_kwargs"] = kwargs
            return client

        def index_factory(**kwargs):
            index = FakeIndex(**kwargs)
            observed["index"] = index
            return index

        collection = provisioning.provision_collection(
            settings_obj=make_settings(),
            client_factory=client_factory,
            index_factory=index_factory,
        )

        self.assertEqual("shared_guide_embeddings_v1", collection)
        self.assertEqual(
            {
                "url": "http://qdrant:6333",
                "api_key": "",
                "timeout_seconds": 5.0,
            },
            observed["client_kwargs"],
        )
        self.assertEqual(
            {
                "client": client,
                "collection": "shared_guide_embeddings_v1",
                "dimension": 768,
            },
            observed["index"].kwargs,
        )
        self.assertEqual(1, observed["index"].ensure_calls)

    def test_rejects_non_versioned_collection_before_client_creation(self) -> None:
        calls = []

        with self.assertRaises(ValueError):
            provisioning.provision_collection(
                settings_obj=make_settings(QDRANT_COLLECTION="default"),
                client_factory=lambda **kwargs: calls.append(kwargs),
                index_factory=FakeIndex,
            )

        self.assertEqual([], calls)

    def test_rejects_non_v1_dimension_before_client_creation(self) -> None:
        calls = []

        with self.assertRaises(ValueError):
            provisioning.provision_collection(
                settings_obj=make_settings(EMBEDDING_DIMENSION=1536),
                client_factory=lambda **kwargs: calls.append(kwargs),
                index_factory=FakeIndex,
            )

        self.assertEqual([], calls)

    def test_main_reports_only_error_class(self) -> None:
        private_error = "sk-private-value https://user:password@qdrant/private"
        errors = io.StringIO()

        with patch.object(
            provisioning,
            "provision_collection",
            side_effect=RuntimeError(private_error),
        ), redirect_stderr(errors):
            exit_code = provisioning.main()

        self.assertEqual(1, exit_code)
        self.assertIn(
            "qdrant_collection status=failed error_class=RuntimeError",
            errors.getvalue(),
        )
        self.assertNotIn(private_error, errors.getvalue())
        self.assertNotIn("password", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
