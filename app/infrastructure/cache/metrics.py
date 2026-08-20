"""线程安全的进程内缓存指标；后续可映射到 OpenTelemetry。"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

from app.infrastructure.cache.models import CacheReadStatus, CacheWriteStatus


@dataclass(frozen=True, slots=True)
class CacheMetricsSnapshot:
    backend: str
    read_requests: int
    hits: int
    misses: int
    bypasses: int
    degraded_reads: int
    invalid_entries: int
    expired_entries: int
    write_requests: int
    writes: int
    skipped_writes: int
    degraded_writes: int
    delete_requests: int
    deletes: int
    degraded_deletes: int
    hit_rate: float

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class CacheMetrics:
    """只记录计数，不记录 Key、地址或缓存值。"""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self._values = {
            "read_requests": 0,
            "hits": 0,
            "misses": 0,
            "bypasses": 0,
            "degraded_reads": 0,
            "invalid_entries": 0,
            "expired_entries": 0,
            "write_requests": 0,
            "writes": 0,
            "skipped_writes": 0,
            "degraded_writes": 0,
            "delete_requests": 0,
            "deletes": 0,
            "degraded_deletes": 0,
        }

    def record_read(self, status: CacheReadStatus, *, reason: str | None = None) -> None:
        with self._lock:
            self._values["read_requests"] += 1
            if status == CacheReadStatus.HIT:
                self._values["hits"] += 1
            elif status == CacheReadStatus.MISS:
                self._values["misses"] += 1
            elif status == CacheReadStatus.BYPASS:
                self._values["bypasses"] += 1
            elif status == CacheReadStatus.DEGRADED:
                self._values["degraded_reads"] += 1
            if reason == "invalid_entry":
                self._values["invalid_entries"] += 1
            elif reason == "expired_entry":
                self._values["expired_entries"] += 1

    def record_write(self, status: CacheWriteStatus) -> None:
        with self._lock:
            self._values["write_requests"] += 1
            if status == CacheWriteStatus.STORED:
                self._values["writes"] += 1
            elif status == CacheWriteStatus.SKIPPED:
                self._values["skipped_writes"] += 1
            elif status == CacheWriteStatus.DEGRADED:
                self._values["degraded_writes"] += 1

    def record_delete(self, *, deleted: bool, degraded: bool = False) -> None:
        with self._lock:
            self._values["delete_requests"] += 1
            if deleted:
                self._values["deletes"] += 1
            if degraded:
                self._values["degraded_deletes"] += 1

    def snapshot(self) -> CacheMetricsSnapshot:
        with self._lock:
            values = dict(self._values)
        denominator = values["hits"] + values["misses"]
        hit_rate = values["hits"] / denominator if denominator else 0.0
        return CacheMetricsSnapshot(
            backend=self.backend,
            **values,
            hit_rate=round(hit_rate, 6),
        )
