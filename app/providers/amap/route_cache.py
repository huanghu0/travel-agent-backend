"""标准化高德路线结果的缓存键契约。"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from app.providers.amap.models import RouteEstimate, RouteLegRequest


class RouteCache(Protocol):
    """注入高德 Provider 的最小路线缓存接口。"""

    def get(self, cache_key: str) -> RouteEstimate | None:
        """返回未过期路线结果；缓存未命中时返回 None。"""

    def set(
        self,
        cache_key: str,
        estimate: RouteEstimate,
        *,
        ttl_seconds: int,
    ) -> None:
        """按指定 TTL 持久化一条路线结果。"""


def route_leg_cache_key(leg: RouteLegRequest) -> str:
    """使用会影响供应商路线结果的字段构建稳定缓存键。"""

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
