"""标准化餐饮搜索快照的缓存键与注入契约。"""

from __future__ import annotations

import hashlib
import json
from app.persistence.interfaces import RestaurantCacheStore
from app.providers.amap.models import GeoPoint, RestaurantSearchSnapshot

# 兼容原有 Provider 导入名；实际契约统一定义在 persistence.interfaces。
RestaurantCache = RestaurantCacheStore


def restaurant_search_cache_key(
    *,
    city: str,
    keywords: str,
    center: GeoPoint,
    radius_meters: int,
    page_size: int,
) -> str:
    """使用所有会影响高德周边餐饮搜索结果的字段构建稳定缓存键。"""

    payload = {
        "version": 1,
        "provider": "amap",
        "city": city.strip().lower(),
        "keywords": keywords.strip().lower(),
        "types": "050000",
        "longitude": f"{center.longitude:.6f}",
        "latitude": f"{center.latitude:.6f}",
        "radius_meters": int(radius_meters),
        "page_size": int(page_size),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
