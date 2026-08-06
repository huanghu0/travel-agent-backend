import unittest
from unittest.mock import Mock, patch

import requests

from app.core.config import settings
from app.providers.amap.client import AmapClient
from app.providers.amap.models import GeoPoint
from app.tools.amap_tools import AmapTools
from app.tools.models import ToolErrorType
from app.tools.registry import ToolResultError


class AmapTimeoutAndClassificationTests(unittest.TestCase):
    @patch("app.tools.amap_tools.requests.get")
    def test_timeout_tuple_is_used_and_timeout_is_retryable(self, request_get):
        request_get.side_effect = requests.Timeout("secret URL should not leak")

        with self.assertRaises(ToolResultError) as caught:
            AmapTools.get_weather("成都")

        self.assertEqual(caught.exception.error_type, ToolErrorType.TIMEOUT)
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(
            request_get.call_args.kwargs["timeout"],
            (settings.AMAP_HTTP_CONNECT_TIMEOUT, settings.AMAP_HTTP_READ_TIMEOUT),
        )

    def _assert_http_status(self, status, error_type, retryable):
        response = Mock()
        response.status_code = status
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch("app.tools.amap_tools.requests.get", return_value=response):
            with self.assertRaises(ToolResultError) as caught:
                AmapTools.get_weather("成都")
        self.assertEqual(caught.exception.error_type, error_type)
        self.assertEqual(caught.exception.retryable, retryable)

    def test_403_is_authorization_and_not_retryable(self):
        self._assert_http_status(403, ToolErrorType.AUTHORIZATION, False)

    def test_429_is_rate_limit_and_retryable(self):
        self._assert_http_status(429, ToolErrorType.RATE_LIMIT, True)

    def test_5xx_is_upstream_and_retryable(self):
        self._assert_http_status(503, ToolErrorType.UPSTREAM, True)


class AmapRouteRequestTests(unittest.TestCase):
    def setUp(self):
        self.response = Mock()
        self.response.raise_for_status.return_value = None
        self.response.json.return_value = {
            "status": "1",
            "info": "OK",
            "route": {"paths": []},
        }
        self.origin = GeoPoint(longitude=104.01, latitude=30.61)
        self.destination = GeoPoint(longitude=104.02, latitude=30.62)

    def test_route_modes_use_v5_endpoints_and_request_cost_fields(self):
        expected_urls = {
            "driving": "https://restapi.amap.com/v5/direction/driving",
            "walking": "https://restapi.amap.com/v5/direction/walking",
            "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
        }

        with patch.object(AmapClient, "http_get", return_value=self.response) as get:
            for mode in ("driving", "walking", "transit"):
                kwargs = {}
                if mode == "transit":
                    kwargs.update(
                        origin_city_code="028",
                        destination_city_code="028",
                    )
                AmapClient.route(
                    origin=self.origin,
                    destination=self.destination,
                    mode=mode,
                    **kwargs,
                )
                url = get.call_args.args[0]
                params = get.call_args.kwargs["params"]
                self.assertEqual(url, expected_urls[mode])
                self.assertEqual(params["show_fields"], "cost")
                self.assertEqual(params["origin"], "104.010000,30.610000")
                self.assertEqual(params["destination"], "104.020000,30.620000")

    def test_transit_city_codes_and_poi_ids_are_sent_as_a_pair(self):
        with patch.object(AmapClient, "http_get", return_value=self.response) as get:
            AmapClient.route(
                origin=self.origin,
                destination=self.destination,
                mode="transit",
                origin_poi_id="poi-a",
                destination_poi_id="poi-b",
                origin_city_code="028",
                destination_city_code="028",
            )

        params = get.call_args.kwargs["params"]
        self.assertEqual(params["city1"], "028")
        self.assertEqual(params["city2"], "028")
        self.assertEqual(params["originpoi"], "poi-a")
        self.assertEqual(params["destinationpoi"], "poi-b")



if __name__ == "__main__":
    unittest.main()
