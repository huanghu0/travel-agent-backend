"""Wait for required container dependencies without importing the application."""

from __future__ import annotations

import math
import os
import socket
import sys
import time
from collections.abc import Callable, Mapping
from typing import TextIO
from urllib.request import Request, urlopen


MAX_WAIT_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 1.0


def probe_mysql(host: str, port: int, timeout_seconds: float) -> bool:
    """Return true after a TCP connection succeeds; authentication is checked later."""

    with socket.create_connection((host, port), timeout=timeout_seconds):
        return True


def probe_qdrant(
    base_url: str,
    api_key: str | None,
    timeout_seconds: float,
) -> bool:
    """Return true only when Qdrant's readiness endpoint returns 2xx."""

    headers = {"Accept": "text/plain"}
    if api_key:
        headers["api-key"] = api_key
    request = Request(
        f"{base_url.rstrip('/')}/readyz",
        headers=headers,
        method="GET",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return 200 <= int(response.status) < 300


def wait_for_dependencies(
    *,
    probes: Mapping[str, Callable[[float], bool]],
    timeout_seconds: float = MAX_WAIT_SECONDS,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    output: TextIO | None = None,
    error_output: TextIO | None = None,
) -> bool:
    """Poll named probes within one shared deadline and emit sanitized state only."""

    timeout_seconds = float(timeout_seconds)
    poll_interval_seconds = float(poll_interval_seconds)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    output = output or sys.stdout
    error_output = error_output or sys.stderr
    deadline = monotonic() + min(float(timeout_seconds), MAX_WAIT_SECONDS)
    pending = dict(probes)

    while pending:
        for name, probe in tuple(pending.items()):
            remaining = deadline - monotonic()
            if remaining <= 0:
                print(
                    "dependency_wait status=timeout pending="
                    + ",".join(sorted(pending)),
                    file=error_output,
                )
                return False
            probe_timeout = min(PROBE_TIMEOUT_SECONDS, remaining)
            try:
                ready = bool(probe(probe_timeout))
            except Exception:
                ready = False
            if ready:
                print(f"dependency={name} status=ready", file=output)
                del pending[name]

        if pending:
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(poll_interval_seconds, remaining))

    return True


def _required_text(name: str, default: str) -> str:
    value = str(os.getenv(name, default) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _mysql_port() -> int:
    value = int(_required_text("MYSQL_PORT", "3306"))
    if not 1 <= value <= 65535:
        raise ValueError("MYSQL_PORT must be between 1 and 65535")
    return value


def _wait_timeout() -> float:
    value = float(_required_text("DEPENDENCY_WAIT_TIMEOUT_SECONDS", "60"))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("DEPENDENCY_WAIT_TIMEOUT_SECONDS must be positive")
    return min(value, MAX_WAIT_SECONDS)


def main() -> int:
    try:
        mysql_host = _required_text("MYSQL_HOST", "127.0.0.1")
        mysql_port = _mysql_port()
        qdrant_url = _required_text("QDRANT_URL", "http://127.0.0.1:6333")
        qdrant_api_key = str(os.getenv("QDRANT_API_KEY", "") or "").strip() or None
        ready = wait_for_dependencies(
            probes={
                "mysql": lambda timeout: probe_mysql(
                    mysql_host,
                    mysql_port,
                    timeout,
                ),
                "qdrant": lambda timeout: probe_qdrant(
                    qdrant_url,
                    qdrant_api_key,
                    timeout,
                ),
            },
            timeout_seconds=_wait_timeout(),
        )
    except Exception as error:
        print(
            f"dependency_wait status=failed error_class={type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
