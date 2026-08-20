"""Reliable checkpoint persistence with bounded retries for transient SQLite locks."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from time import sleep
from typing import Callable

from app.agent_runtime.state import AgentState
from app.persistence.interfaces import AgentStateStore


@dataclass(frozen=True)
class CheckpointRetryEvent:
    """Audit information for one transient checkpoint retry."""

    attempt: int
    max_attempts: int
    delay_seconds: float
    error: str


class CheckpointPolicy:
    """Retry only transient SQLite locked/busy errors with a bounded backoff."""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        base_delay_seconds: float = 0.05,
        max_delay_seconds: float = 0.5,
        sleep_fn: Callable[[float], None] = sleep,
    ):
        if max_attempts < 1:
            raise ValueError("checkpoint max_attempts must be at least 1")
        if base_delay_seconds < 0 or max_delay_seconds < 0:
            raise ValueError("checkpoint retry delays cannot be negative")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError(
                "checkpoint max_delay_seconds cannot be less than base_delay_seconds"
            )
        self.max_attempts = max_attempts
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.sleep_fn = sleep_fn
        self.retry_events: list[CheckpointRetryEvent] = []

    def save(self, store: AgentStateStore, state: AgentState) -> None:
        """Persist state, retrying only SQLite lock contention."""

        for attempt in range(1, self.max_attempts + 1):
            try:
                store.save_state(state)
                return
            except sqlite3.OperationalError as exc:
                if not self.is_transient_lock_error(exc) or attempt >= self.max_attempts:
                    raise
                delay = min(
                    self.max_delay_seconds,
                    self.base_delay_seconds * (2 ** (attempt - 1)),
                )
                self.retry_events.append(
                    CheckpointRetryEvent(
                        attempt=attempt,
                        max_attempts=self.max_attempts,
                        delay_seconds=delay,
                        error=self.safe_error_message(exc),
                    )
                )
                if delay > 0:
                    self.sleep_fn(delay)

    @staticmethod
    def is_transient_lock_error(exc: sqlite3.OperationalError) -> bool:
        message = " ".join(str(exc).lower().split())
        return "database is locked" in message or "database is busy" in message

    @staticmethod
    def safe_error_message(exc: Exception) -> str:
        return (" ".join(str(exc).split()) or exc.__class__.__name__)[:500]
