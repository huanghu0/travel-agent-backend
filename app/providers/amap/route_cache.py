"""Cache-key contract for normalized Amap route estimates."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from app.providers.amap.models import RouteEstimate, RouteLegRequest


class RouteCache(Protocol):
    """Minimal cache interface injected into the Amap provider."""

    def get(self, cache_key: str) -> RouteEstimate | None:
        """Return a non-expired estimate, or None on a cache miss."""

    def set(
        self,
        cache_key: str,
        estimate: RouteEstimate,
        *,
        ttl_seconds: int,
    ) -> None:
        """Persist one estimate for the requested TTL."""


def route_leg_cache_key(leg: RouteLegRequest) -> str:
    """Build a stable key from fields that affect the provider route result."""

    payload = {
        "version": 1,
        "provider": "amap",
        "mode": leg.mode,
        "origin": {
            "poi_id": leg.origin.poi_id.strip(),
            "longitude": f"{leg.origin.location.longitude:.6f}",
            "latitude": f"{leg.origin.location.latitude:.6f}",
            "city_code": leg.origin.city_code.strip(),
        },
        "destination": {
            "poi_id": leg.destination.poi_id.strip(),
            "longitude": f"{leg.destination.location.longitude:.6f}",
            "latitude": f"{leg.destination.location.latitude:.6f}",
            "city_code": leg.destination.city_code.strip(),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
