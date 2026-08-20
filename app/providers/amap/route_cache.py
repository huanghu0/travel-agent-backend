"""标准化高德路线结果的缓存键契约。"""

from __future__ import annotations

import hashlib
import json
from app.persistence.interfaces import RouteCacheStore
from app.providers.amap.models import RouteEstimate, RouteLegRequest

# 兼容原有 Provider 导入名；实际契约统一定义在 persistence.interfaces。
RouteCache = RouteCacheStore


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
