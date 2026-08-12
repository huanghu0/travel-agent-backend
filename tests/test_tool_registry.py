import unittest

from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.providers.amap import AmapClient, AmapProviderClient, GeoPoint
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

    def test_nearby_attraction_tool_uses_standardized_provider_without_llm(self):
        class RawProvider:
            calls = []

            @classmethod
            def around_search(
                cls,
                *,
                location,
                city,
                keywords,
                radius_meters,
                page,
                page_size,
            ):
                cls.calls.append(
                    (location, city, keywords, radius_meters, page, page_size)
                )
                return {
                    "status": "1",
                    "info": "OK",
                    "pois": [
                        {
                            "id": "nearby-1",
                            "name": "West Lake Culture Square",
                            "address": "Downtown",
                            "location": "120.165,30.275",
                            "type": "scenic attraction",
                            "biz_ext": {"rating": "4.7"},
                        }
                    ],
                }

            @staticmethod
            def text_search(*, keywords, city):
                return {"status": "1", "info": "OK", "pois": []}

            @staticmethod
            def get_weather(city):
                return {"status": "1", "info": "OK", "forecasts": []}

        provider = AmapProviderClient(raw_client=RawProvider)
        registry = build_trip_tool_registry(
            planner_agent=object(),
            map_provider=provider,
        )
        payload = {
            "city": "Hangzhou",
            "keywords": "nature",
            "center": {"longitude": 120.16, "latitude": 30.25},
            "radius_meters": 5000,
            "page": 2,
            "page_size": 12,
            "day_index": 0,
            "attraction_index": 1,
            "target_attraction_name": "Remote Place",
            "anchor_names": ["West Lake", "Hotel"],
        }

        result = registry.execute("supplement_attractions", payload)

        self.assertTrue(result.success)
        self.assertEqual(result.data["provider"], "amap")
        self.assertEqual(result.data["center"], payload["center"])
        self.assertEqual(result.data["radius_meters"], 5000)
        self.assertEqual(result.data["page"], 2)
        self.assertEqual(result.data["page_size"], 12)
        self.assertEqual(result.data["candidates"][0]["poi_id"], "nearby-1")
        self.assertNotIn("pois", result.data)
        self.assertEqual(registry.llm_call_cost("supplement_attractions"), 0)
        call = RawProvider.calls[-1]
        self.assertEqual(call[0], GeoPoint(longitude=120.16, latitude=30.25))
        self.assertEqual(call[1:], ("Hangzhou", "nature", 5000, 2, 12))

    def test_raw_amap_around_search_uses_distance_sorted_bounded_parameters(self):
        captured = {}

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"status": "1", "pois": []}

        previous = AmapClient.http_get

        def fake_get(url, *, params, timeout):
            captured.update(url=url, params=params, timeout=timeout)
            return Response()

        AmapClient.http_get = staticmethod(fake_get)
        try:
            AmapClient.around_search(
                location=GeoPoint(longitude=120.16, latitude=30.25),
                city="Hangzhou",
                keywords="attraction",
                radius_meters=60000,
                page=0,
                page_size=30,
            )
        finally:
            AmapClient.http_get = previous

        self.assertTrue(captured["url"].endswith("/v5/place/around"))
        self.assertEqual(captured["params"]["location"], "120.160000,30.250000")
        self.assertEqual(captured["params"]["city_limit"], "true")
        self.assertEqual(captured["params"]["sortrule"], "distance")
        self.assertEqual(captured["params"]["radius"], 50000)
        self.assertEqual(captured["params"]["page_size"], 25)
        self.assertEqual(captured["params"]["page_num"], 1)

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
                "supplement_attractions",
                "get_weather",
                "search_hotels",
                "search_pois",
                "search_restaurants",
                "get_poi_detail",
                "resolve_location",
                "estimate_routes",
                "generate_plan",
                "repair_plan",
            },
        )
        self.assertEqual(main.trip_tool_registry.llm_call_cost("search_attractions"), 0)
        self.assertEqual(
            main.trip_tool_registry.llm_call_cost("supplement_attractions"),
            0,
        )
        self.assertEqual(main.trip_tool_registry.llm_call_cost("get_weather"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("search_hotels"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("search_pois"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("search_restaurants"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("get_poi_detail"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("resolve_location"), 0)
        self.assertEqual(main.trip_tool_registry.llm_call_cost("estimate_routes"), 0)


if __name__ == "__main__":
    unittest.main()