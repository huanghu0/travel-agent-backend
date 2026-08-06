import unittest

from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.tools import (
    ToolDefinition,
    ToolErrorType,
    ToolRegistry,
    ToolResultError,
    build_trip_tool_registry,
)


class EchoInput(BaseModel):
    text: str = Field(min_length=1)


class EchoOutput(BaseModel):
    value: str


class ToolRegistryTests(unittest.TestCase):
    def test_registered_tool_validates_input_and_normalizes_output(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo validated text",
                input_model=EchoInput,
                handler=lambda value: {"value": value.text},
                output_model=EchoOutput,
            )
        )

        result = registry.execute("echo", {"text": "hello"})

        self.assertTrue(result.success)
        self.assertEqual(result.tool_name, "echo")
        self.assertEqual(result.data, {"value": "hello"})
        self.assertIsNone(result.error_type)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_invalid_input_does_not_call_handler_and_is_not_retryable(self):
        calls = []
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo validated text",
                input_model=EchoInput,
                handler=lambda value: calls.append(value),
            )
        )

        result = registry.execute("echo", {"text": ""})

        self.assertFalse(result.success)
        self.assertEqual(calls, [])
        self.assertEqual(result.error_type, ToolErrorType.INVALID_INPUT)
        self.assertFalse(result.retryable)

    def test_unknown_tool_is_rejected_by_whitelist(self):
        result = ToolRegistry().execute("shell", {"command": "whoami"})

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, ToolErrorType.TOOL_NOT_FOUND)
        self.assertFalse(result.retryable)
        self.assertNotIn("whoami", result.error)

    def test_duplicate_registration_is_rejected(self):
        registry = ToolRegistry()
        definition = ToolDefinition(
            name="echo",
            description="Echo",
            input_model=EchoInput,
            handler=lambda value: value.text,
        )
        registry.register(definition)

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(definition)

    def test_known_tool_error_preserves_retry_semantics(self):
        registry = ToolRegistry()

        def fail(_):
            raise ToolResultError(
                "temporary upstream failure",
                error_type=ToolErrorType.UPSTREAM,
                retryable=True,
            )

        registry.register(
            ToolDefinition(
                name="unstable",
                description="Fails temporarily",
                input_model=EchoInput,
                handler=fail,
            )
        )

        result = registry.execute("unstable", {"text": "go"})

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, ToolErrorType.UPSTREAM)
        self.assertTrue(result.retryable)

    def test_authorization_exception_is_not_retryable(self):
        registry = ToolRegistry()

        def fail(_):
            raise RuntimeError("Error code: 403 - token cannot use model")

        registry.register(
            ToolDefinition(
                name="protected",
                description="Protected tool",
                input_model=EchoInput,
                handler=fail,
            )
        )

        result = registry.execute("protected", {"text": "go"})

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, ToolErrorType.AUTHORIZATION)
        self.assertFalse(result.retryable)

    def test_describe_exposes_schemas_but_not_handlers(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo",
                description="Echo",
                input_model=EchoInput,
                handler=lambda value: value.text,
                output_model=EchoOutput,
            )
        )

        descriptor = registry.describe()[0]

        self.assertEqual(descriptor.name, "echo")
        self.assertIn("properties", descriptor.input_schema)
        self.assertIn("properties", descriptor.output_schema)
        self.assertNotIn("handler", descriptor.model_dump())

    def test_direct_map_tools_do_not_consume_llm_calls(self):
        class RecordingMapProvider:
            calls = []

            @classmethod
            def text_search(cls, *, keywords, city):
                cls.calls.append(("text_search", keywords, city))
                return {"status": "1", "info": "OK", "pois": []}

            @classmethod
            def get_weather(cls, city):
                cls.calls.append(("get_weather", city))
                return {"status": "1", "info": "OK", "forecasts": []}

        registry = build_trip_tool_registry(
            planner_agent=object(),
            map_provider=RecordingMapProvider,
        )

        attraction = registry.execute(
            "search_attractions",
            {"city": "Chengdu", "preferences": ["history", "food"]},
        )
        weather = registry.execute("get_weather", {"city": "Chengdu"})
        hotel = registry.execute("search_hotels", {"city": "Chengdu"})

        self.assertTrue(attraction.success)
        self.assertTrue(weather.success)
        self.assertTrue(hotel.success)
        self.assertEqual(attraction.data["provider"], "amap")
        self.assertEqual(attraction.data["candidates"], [])
        self.assertEqual(weather.data["forecasts"], [])
        self.assertEqual(hotel.data["candidates"], [])
        self.assertNotIn("pois", attraction.data)
        self.assertEqual(
            RecordingMapProvider.calls,
            [
                ("text_search", "history,food", "Chengdu"),
                ("get_weather", "Chengdu"),
                ("text_search", "\u9152\u5e97", "Chengdu"),
            ],
        )
        self.assertEqual(registry.llm_call_cost("search_attractions"), 0)
        self.assertEqual(registry.llm_call_cost("get_weather"), 0)
        self.assertEqual(registry.llm_call_cost("search_hotels"), 0)
        self.assertEqual(registry.llm_call_cost("estimate_routes"), 0)
        self.assertEqual(registry.llm_call_cost("generate_plan"), 1)
        self.assertEqual(registry.llm_call_cost("repair_plan"), 1)

    def test_empty_route_batch_returns_without_provider_call(self):
        class Provider:
            @staticmethod
            def search_attractions(*, city, keywords):
                raise AssertionError("not used")

            @staticmethod
            def search_hotels(*, city, keywords):
                raise AssertionError("not used")

            @staticmethod
            def get_weather(city):
                raise AssertionError("not used")

            @staticmethod
            def estimate_routes(**kwargs):
                raise AssertionError("empty legs must not call provider")

        registry = build_trip_tool_registry(
            planner_agent=object(),
            map_provider=Provider(),
        )
        result = registry.execute(
            "estimate_routes",
            {
                "city": "Chengdu",
                "plan_fingerprint": "fingerprint",
                "legs": [],
            },
        )

        self.assertTrue(result.success)
        self.assertEqual(result.data["requested_legs"], 0)
        self.assertEqual(result.data["routes"], [])

    def test_amap_authorization_payload_preserves_provider_diagnostics(self):
        class FailingMapProvider:
            @staticmethod
            def text_search(*, keywords, city):
                return {
                    "status": "0",
                    "info": "INVALID_USER_SIGNATURE",
                    "infocode": "10007",
                }

            @staticmethod
            def get_weather(city):
                raise AssertionError("not used")

        registry = build_trip_tool_registry(
            planner_agent=object(),
            map_provider=FailingMapProvider,
        )

        result = registry.execute(
            "search_attractions",
            {"city": "Chengdu", "preferences": []},
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, ToolErrorType.AUTHORIZATION)
        self.assertFalse(result.retryable)
        self.assertEqual(result.provider_code, "10007")
        self.assertEqual(result.provider_message, "INVALID_USER_SIGNATURE")
        self.assertIn("infocode=10007", result.error)

    def test_public_tool_endpoint_lists_only_registered_travel_tools(self):
        import main

        response = TestClient(main.app).get("/api/agent/tools")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {item["name"] for item in response.json()},
            {
                "search_attractions",
                "get_weather",
                "search_hotels",
                "estimate_routes",
                "generate_plan",
                "repair_plan",
            },
        )
        self.assertEqual(main.trip_tool_registry.llm_call_cost("search_attractions"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("get_weather"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("search_hotels"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("estimate_routes"), 0)


if __name__ == "__main__":
    unittest.main()