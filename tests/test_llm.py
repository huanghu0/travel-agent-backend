import unittest
from types import SimpleNamespace

from app.core.llm import AnthropicLLM, LLMOutputTruncatedError, ResponsesLLM
from app.tools.amap_tools import parse_tool_call
from pydantic import BaseModel


class StructuredResult(BaseModel):
    days: list[str]
    overall_suggestions: str


class FakeResponses:
    def __init__(self, response):
        self.response = response
        self.request = None
        self.method = None

    def create(self, **kwargs):
        self.method = "create"
        self.request = kwargs
        return self.response

    def parse(self, **kwargs):
        self.method = "parse"
        self.request = kwargs
        return self.response


class FakeResponsesClient:
    def __init__(self, response):
        self.responses = FakeResponses(response)


class CreateOnlyResponses:
    def __init__(self, response):
        self.response = response
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return self.response


class CreateOnlyResponsesClient:
    def __init__(self, response):
        self.responses = CreateOnlyResponses(response)


class ResponsesStructuredOutputTests(unittest.TestCase):
    def test_uses_sdk_parse_for_pydantic_response_model(self):
        parsed = StructuredResult(days=["day 1"], overall_suggestions="tip")
        client = FakeResponsesClient(
            SimpleNamespace(status="completed", output_parsed=parsed)
        )
        result = ResponsesLLM(client=client, model="test-model").invoke(
            "system",
            "user",
            response_model=StructuredResult,
        )
        self.assertEqual(client.responses.method, "parse")
        self.assertIs(client.responses.request["text_format"], StructuredResult)
        self.assertEqual(StructuredResult.model_validate_json(result), parsed)

    def test_create_fallback_sends_json_schema(self):
        client = CreateOnlyResponsesClient(
            SimpleNamespace(
                status="completed",
                output_text='{"days": [], "overall_suggestions": "tip"}',
            )
        )
        ResponsesLLM(client=client, model="test-model").invoke(
            "system",
            "user",
            response_model=StructuredResult,
        )
        output_format = client.responses.request["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertEqual(output_format["name"], "structuredresult")
        self.assertIn("days", output_format["schema"]["properties"])


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return self.response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


class SequenceMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class SequenceClient:
    def __init__(self, responses):
        self.messages = SequenceMessages(responses)


class AnthropicLLMTextExtractionTests(unittest.TestCase):
    def invoke(self, response):
        client = FakeClient(response)
        result = AnthropicLLM(
            client=client,
            model="test-model",
            max_tokens=1024,
        ).invoke("system", "user")
        self.assertEqual(client.messages.request["system"], "system")
        self.assertEqual(client.messages.request["messages"][0]["content"], "user")
        return result

    def test_official_text_block(self):
        response = SimpleNamespace(
            id="msg_1",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="answer")],
        )
        self.assertEqual(self.invoke(response), "answer")

    def test_untyped_text_block(self):
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(text="answer")],
        )
        self.assertEqual(self.invoke(response), "answer")

    def test_dictionary_response(self):
        response = {
            "id": "msg_2",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "answer"}],
        }
        self.assertEqual(self.invoke(response), "answer")

    def test_single_dictionary_content_block(self):
        response = {
            "stop_reason": "end_turn",
            "content": {"text": "answer"},
        }
        self.assertEqual(self.invoke(response), "answer")

    def test_string_content(self):
        self.assertEqual(
            self.invoke({"stop_reason": "end_turn", "content": "answer"}),
            "answer",
        )

    def test_output_text_fallback(self):
        self.assertEqual(
            self.invoke({"content": [], "output_text": "answer"}),
            "answer",
        )

    def test_completion_fallback(self):
        self.assertEqual(
            self.invoke({"content": [], "completion": "answer"}),
            "answer",
        )

    def test_response_model_adds_compact_required_fields_only(self):
        client = FakeClient(
            SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text="{}")],
            )
        )
        AnthropicLLM(
            client=client,
            model="test-model",
            max_tokens=1024,
        ).invoke(
            "system",
            "user",
            response_model=StructuredResult,
        )
        system = client.messages.request["system"]
        self.assertIn("days", system)
        self.assertIn("overall_suggestions", system)
        self.assertNotIn('"$defs"', system)
        self.assertLess(len(system), 500)

    def test_native_tool_use_is_normalized_for_existing_agents(self):
        response = SimpleNamespace(
            id="msg_tool",
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="thinking", thinking="private"),
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_1",
                    name="amap_maps_text_search",
                    input={"keywords": "??,??", "city": "??"},
                ),
            ],
        )
        normalized = self.invoke(response)
        tool_name, params = parse_tool_call(normalized)
        self.assertEqual(tool_name, "amap_maps_text_search")
        self.assertEqual(params, {"keywords": "??,??", "city": "??"})
        self.assertNotIn("private", normalized)

    def test_native_tool_alias_is_supported(self):
        response = {
            "stop_reason": "tool_use",
            "content": {
                "type": "tool_use",
                "name": "maps_weather",
                "input": {"city": "??"},
            },
        }
        tool_name, params = parse_tool_call(self.invoke(response))
        self.assertEqual(tool_name, "amap_maps_weather")
        self.assertEqual(params, {"city": "??"})

    def test_structured_output_continues_after_unexpected_tool_use(self):
        first = SimpleNamespace(
            id="msg_tool",
            stop_reason="tool_use",
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_shell",
                    name="shell",
                    input={"command": "echo generating plan"},
                )
            ],
        )
        second = SimpleNamespace(
            id="msg_json",
            stop_reason="end_turn",
            content=[
                SimpleNamespace(
                    type="text",
                    text='{"days": [], "overall_suggestions": "tip"}',
                )
            ],
        )
        client = SequenceClient([first, second])
        result = AnthropicLLM(
            client=client,
            model="test-model",
            max_tokens=1024,
        ).invoke(
            "system",
            "user",
            response_model=StructuredResult,
        )

        self.assertEqual(len(client.messages.requests), 2)
        self.assertEqual(
            StructuredResult.model_validate_json(result),
            StructuredResult(days=[], overall_suggestions="tip"),
        )
        retry_messages = client.messages.requests[1]["messages"]
        self.assertEqual([message["role"] for message in retry_messages], ["user", "assistant", "user"])
        tool_result = retry_messages[-1]["content"][0]
        self.assertEqual(tool_result["tool_use_id"], "toolu_shell")
        self.assertTrue(tool_result["is_error"])

    def test_repeated_structured_tool_use_raises_safe_error(self):
        tool_response = {
            "id": "msg_tool",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_shell",
                    "name": "shell",
                    "input": {"command": "sensitive command"},
                }
            ],
        }
        client = SequenceClient([tool_response, tool_response])
        with self.assertRaisesRegex(
            RuntimeError,
            r"tool call instead of structured JSON after retry.*tool_names=shell",
        ) as caught:
            AnthropicLLM(
                client=client,
                model="test-model",
                max_tokens=1024,
            ).invoke(
                "system",
                "user",
                response_model=StructuredResult,
            )
        self.assertNotIn("sensitive command", str(caught.exception))

    def test_thinking_is_not_returned_and_error_is_diagnostic(self):
        response = {
            "id": "msg_thinking",
            "stop_reason": "end_turn",
            "content": [{"type": "thinking", "thinking": "private"}],
        }
        with self.assertRaisesRegex(
            RuntimeError,
            r"no text output .*stop_reason=end_turn.*content_types=thinking",
        ) as caught:
            self.invoke(response)
        self.assertNotIn("private", str(caught.exception))

    def test_max_tokens_is_reported_as_truncation(self):
        response = {"stop_reason": "max_tokens", "content": []}
        with self.assertRaises(LLMOutputTruncatedError):
            self.invoke(response)


if __name__ == "__main__":
    unittest.main()
