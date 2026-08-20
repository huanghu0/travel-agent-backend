"""CacheStore 共享输入校验，避免不同后端产生配置相关行为。"""

from __future__ import annotations


MAX_CACHE_KEY_BYTES = 512


def validate_cache_key(key: str) -> str:
    """校验缓存 Key；不修改 Key，避免调用方误以为发生了规范化。"""

    if not isinstance(key, str) or not key.strip():
        raise ValueError("缓存 Key 必须是非空字符串")
    if len(key.encode("utf-8")) > MAX_CACHE_KEY_BYTES:
        raise ValueError(f"缓存 Key 不能超过 {MAX_CACHE_KEY_BYTES} 字节")
    return key
