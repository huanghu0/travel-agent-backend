import logging
import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.rag.embedding import (
    DashScopeEmbeddingClient,
    EmbeddingConfigurationError,
    EmbeddingUnavailableError,
    InvalidEmbeddingError,
)


class FakeEmbeddings:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.embeddings = FakeEmbeddings(response=response, error=error)


def response_for(values):
    return SimpleNamespace(data=[SimpleNamespace(embedding=values)])


class DashScopeEmbeddingTests(unittest.TestCase):
    def make_client(self, fake_client, **kwargs):
        values = dict(
            api_key="fake-dashscope-key",
            base_url=(
                "https://workspace.cn-beijing.maas.aliyuncs.com/"
                "compatible-mode/v1"
            ),
            model="qwen3.7-text-embedding",
            dimension=768,
            timeout_seconds=12.5,
            max_attempts=4,
        )
        values.update(kwargs)
        return DashScopeEmbeddingClient(client=fake_client, **values)

    def test_embed_sends_exact_model_input_and_dimension(self):
        fake = FakeClient(response_for([1.0] * 768))

        result = self.make_client(fake).embed("safe input")

        self.assertEqual(result, [1.0] * 768)
        self.assertEqual(
            fake.embeddings.calls[0],
            {
                "model": "qwen3.7-text-embedding",
                "input": "safe input",
                "dimensions": 768,
            },
        )

    def test_embed_converts_numeric_values_to_floats(self):
        fake = FakeClient(response_for([1] * 768))
        self.assertTrue(
            all(
                isinstance(value, float)
                for value in self.make_client(fake).embed("x")
            )
        )

    def test_invalid_embedding_values_raise_stable_error(self):
        invalid_values = [
            None,
            [],
            [1.0] * 767,
            [math.nan] + [1.0] * 767,
            [math.inf] + [1.0] * 767,
            ["not-a-number"] + [1.0] * 767,
        ]
        for values in invalid_values:
            with self.subTest(values=repr(values)[:30]):
                response = (
                    response_for(values)
                    if values is not None
                    else SimpleNamespace(data=None)
                )
                with self.assertRaises(InvalidEmbeddingError):
                    self.make_client(FakeClient(response)).embed(
                        "private request text"
                    )

    def test_provider_timeout_rate_limit_and_server_errors_are_unavailable(self):
        for error in (
            TimeoutError("private request text"),
            RuntimeError("429 private request text"),
            RuntimeError("503 private request text"),
        ):
            with self.subTest(error=str(error)):
                with self.assertRaises(EmbeddingUnavailableError) as raised:
                    self.make_client(FakeClient(error=error)).embed(
                        "private request text"
                    )
                self.assertNotIn("private request text", str(raised.exception))
                self.assertNotIn("fake-dashscope-key", str(raised.exception))

    def test_provider_client_errors_are_configuration_errors(self):
        for status in (400, 401, 403):
            with self.subTest(status=status):
                error = RuntimeError(f"{status} private request text")
                with self.assertRaises(EmbeddingConfigurationError) as raised:
                    self.make_client(FakeClient(error=error)).embed(
                        "private request text"
                    )
                self.assertNotIn("private request text", str(raised.exception))
                self.assertNotIn("fake-dashscope-key", str(raised.exception))

    def test_failures_log_only_safe_metadata(self):
        fake = FakeClient(error=RuntimeError("429 private request text"))
        with self.assertLogs("app.rag.embedding", level=logging.INFO) as captured:
            with self.assertRaises(EmbeddingUnavailableError):
                self.make_client(fake).embed("private request text")
        message = "\n".join(captured.output)
        for field in (
            "dashscope_embedding",
            "model=qwen3.7-text-embedding",
            "outcome=error",
            "duration_ms=",
            "error_kind=unavailable",
        ):
            self.assertIn(field, message)
        self.assertNotIn("private request text", message)
        self.assertNotIn("fake-dashscope-key", message)

    def test_default_client_uses_exact_sdk_configuration(self):
        with patch("app.rag.embedding.OpenAI") as constructor:
            DashScopeEmbeddingClient(
                api_key="fake-dashscope-key",
                base_url=(
                    "https://workspace.cn-beijing.maas.aliyuncs.com/"
                    "compatible-mode/v1"
                ),
                model="qwen3.7-text-embedding",
                dimension=768,
                timeout_seconds=12.5,
                max_attempts=4,
            )

        self.assertEqual(
            constructor.call_args.kwargs,
            {
                "api_key": "fake-dashscope-key",
                "base_url": (
                    "https://workspace.cn-beijing.maas.aliyuncs.com/"
                    "compatible-mode/v1"
                ),
                "timeout": 12.5,
                "max_retries": 3,
            },
        )


if __name__ == "__main__":
    unittest.main()
