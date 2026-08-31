# Backend Compose Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the backend in a pinned Python 3.12/OpenSSL 3 image, add bounded MySQL/Qdrant readiness and safe Qdrant provisioning, then deploy it beside the existing MySQL and Qdrant services with loopback-only API access and staged feature enablement.

**Architecture:** Repository-owned code contains only the generic image build, standard-library readiness gate, Qdrant provisioning command, tests, and an operations runbook. The server keeps `compose.yaml`, `infra.env`, and `backend.env` outside Git; Compose supplies one private service-discovery network, while the host publishes only `127.0.0.1:8000`. Schema migration and Qdrant provisioning are explicit one-off operations before the backend starts, and sharing is enabled before RAG.

**Tech Stack:** Docker Engine 26.1.4, Docker Compose 2.27.1, `ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm`, Python 3.12, FastAPI/Uvicorn, Alembic, MySQL 8.4, Qdrant 1.19, `unittest`.

## Global Constraints

- Base image must remain `ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm@sha256:19a8c92b461bbc32e8bd30c15132cec1d16c49c61f4359c9225262938485f513`.
- Backend host publication must remain exactly `127.0.0.1:8000:8000`; do not expose it on `0.0.0.0` at the host boundary.
- Backend-to-service addresses are `mysql:3306` and `http://qdrant:6333` on the Compose network.
- `compose.yaml`, `infra.env`, and `backend.env` remain server-only under `/home/aicreator/apps/travel-agent-infra` and must never be staged or committed.
- Preserve the existing MySQL and Qdrant named volumes; never run `docker compose down -v` during deployment or rollback.
- Do not put MySQL passwords, JWT secrets, DashScope keys/base URLs, LLM keys/base URLs, Qdrant keys, or server-specific values in the image, Git, test fixtures, command output, or logs.
- Preserve the user's existing uncommitted `.env.example` and `docker-compose.qdrant.yml`; do not edit or stage either file.
- The shared-guide V1 collection is exactly `shared_guide_embeddings_v1`, one unnamed 768-dimensional vector, cosine distance.
- Alembic must reach `f4c2a81d9e30 (head)` before either sharing or RAG is enabled.
- Operational rollback must never run `alembic downgrade`; keep the applied schema and business rows intact.
- First deployment starts with `SHARE_SQUARE_ENABLED=false` and `RAG_ENABLED=false`, then enables sharing first and RAG second.
- Do not add Nginx, Redis, TLS termination, image publishing, automatic Alembic startup migration, or automatic collection deletion/recreation.
- Run only the targeted tests and static/build/smoke checks listed here; do not automatically run the full test suite.
- Every Git commit must stage explicit paths and must be inspected with `git diff --cached --name-status` before committing. Do not push unless the user explicitly requests it.

---

## File Map

### Repository files created

- `scripts/wait_for_dependencies.py` — bounded, sanitized MySQL TCP and Qdrant `/readyz` gate used before Uvicorn imports `main.py`.
- `scripts/ensure_shared_guide_collection.py` — validates the explicit V1 collection name/dimension and delegates idempotent creation/schema checking to `QdrantSharedGuideIndex`.
- `tests/test_wait_for_dependencies.py` — deterministic readiness, timeout, header, and log-redaction tests.
- `tests/test_qdrant_provisioning.py` — provisioning wiring, validation, and sanitized-failure tests.
- `tests/test_container_image_contract.py` — static contract for the pinned base image, explicit copy set, startup command, and ignore rules.
- `Dockerfile` — reproducible backend image with no runtime secrets.
- `.dockerignore` — excludes credentials, local state, tests, documentation, caches, and editor files from the build context.

### Repository files modified

- `.gitignore` — ignores local/server-only Compose and environment filenames so service configuration is not accidentally committed.
- `docs/shared-guide-rag-operations.md` — records the exact server-only Compose block, first-deployment commands, staged enablement, diagnostics, and rollback.

### Server-only files modified but never committed

- `/home/aicreator/apps/travel-agent-infra/compose.yaml` — adds the backend service to the existing MySQL/Qdrant project.
- `/home/aicreator/apps/travel-agent-infra/backend.env` — injects backend runtime settings and secrets with mode `0600`.
- `/home/aicreator/apps/travel-agent-infra/infra.env` — remains the existing MySQL Compose interpolation file with mode `0600`.

---

### Task 1: Add the bounded dependency readiness gate

**Files:**
- Create: `tests/test_wait_for_dependencies.py`
- Create: `scripts/wait_for_dependencies.py`

**Interfaces:**
- Consumes: `MYSQL_HOST`, `MYSQL_PORT`, `QDRANT_URL`, optional `QDRANT_API_KEY`, and optional `DEPENDENCY_WAIT_TIMEOUT_SECONDS` from the process environment.
- Produces: `probe_mysql(host: str, port: int, timeout_seconds: float) -> bool`, `probe_qdrant(base_url: str, api_key: str | None, timeout_seconds: float) -> bool`, `wait_for_dependencies(...) -> bool`, and `main() -> int`.
- Exit codes: `0` when both dependencies are ready, `1` on bounded timeout, `2` on invalid local configuration.

- [ ] **Step 1: Write the failing readiness tests**

Create `tests/test_wait_for_dependencies.py`:

```python
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from scripts import wait_for_dependencies as readiness


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class DependencyReadinessTests(unittest.TestCase):
    def test_waits_until_mysql_and_qdrant_are_both_ready(self) -> None:
        clock = FakeClock()
        mysql_states = iter((False, True))
        output = io.StringIO()
        errors = io.StringIO()

        ready = readiness.wait_for_dependencies(
            probes={
                "mysql": lambda _timeout: next(mysql_states),
                "qdrant": lambda _timeout: True,
            },
            timeout_seconds=3,
            poll_interval_seconds=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            output=output,
            error_output=errors,
        )

        self.assertTrue(ready)
        self.assertEqual(
            [
                "dependency=qdrant status=ready",
                "dependency=mysql status=ready",
            ],
            output.getvalue().splitlines(),
        )
        self.assertEqual("", errors.getvalue())

    def test_timeout_reports_only_dependency_names(self) -> None:
        clock = FakeClock()
        output = io.StringIO()
        errors = io.StringIO()
        private_error = "https://user:password@qdrant/private sk-private-value"

        def unavailable(_timeout: float) -> bool:
            raise RuntimeError(private_error)

        ready = readiness.wait_for_dependencies(
            probes={"mysql": unavailable, "qdrant": unavailable},
            timeout_seconds=2,
            poll_interval_seconds=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            output=output,
            error_output=errors,
        )

        self.assertFalse(ready)
        self.assertEqual("", output.getvalue())
        self.assertIn(
            "dependency_wait status=timeout pending=mysql,qdrant",
            errors.getvalue(),
        )
        self.assertNotIn(private_error, errors.getvalue())
        self.assertNotIn("password", errors.getvalue())

    def test_qdrant_probe_uses_readyz_and_optional_api_key(self) -> None:
        with patch(
            "scripts.wait_for_dependencies.urlopen",
            return_value=Response(),
        ) as urlopen:
            ready = readiness.probe_qdrant(
                "http://qdrant:6333/",
                "qdrant-test-key",
                1.5,
            )

        self.assertTrue(ready)
        request = urlopen.call_args.args[0]
        self.assertEqual("http://qdrant:6333/readyz", request.full_url)
        self.assertEqual("qdrant-test-key", request.get_header("Api-key"))
        self.assertEqual(1.5, urlopen.call_args.kwargs["timeout"])

    def test_wait_is_capped_at_sixty_seconds(self) -> None:
        clock = FakeClock()

        ready = readiness.wait_for_dependencies(
            probes={"mysql": lambda _timeout: False},
            timeout_seconds=600,
            poll_interval_seconds=10,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            output=io.StringIO(),
            error_output=io.StringIO(),
        )

        self.assertFalse(ready)
        self.assertEqual(60.0, clock.now)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the readiness tests and verify they fail**

Run:

```powershell
python -m unittest discover -s tests -p "test_wait_for_dependencies.py" -v
```

Expected: FAIL because `scripts.wait_for_dependencies` does not exist.

- [ ] **Step 3: Implement the standard-library readiness gate**

Create `scripts/wait_for_dependencies.py`:

```python
"""Wait for required container dependencies without importing the application."""

from __future__ import annotations

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

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    output = output or sys.stdout
    error_output = error_output or sys.stderr
    deadline = monotonic() + min(float(timeout_seconds), MAX_WAIT_SECONDS)
    pending = dict(probes)

    while pending:
        remaining = deadline - monotonic()
        if remaining <= 0:
            print(
                "dependency_wait status=timeout pending="
                + ",".join(sorted(pending)),
                file=error_output,
            )
            return False

        probe_timeout = min(PROBE_TIMEOUT_SECONDS, max(0.1, remaining))
        for name, probe in tuple(pending.items()):
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
    if value <= 0:
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
```

- [ ] **Step 4: Run the readiness tests and verify they pass**

Run:

```powershell
python -m unittest discover -s tests -p "test_wait_for_dependencies.py" -v
```

Expected: four tests PASS; no network service is required because all probes are deterministic or mocked.

- [ ] **Step 5: Check syntax and whitespace**

Run:

```powershell
python -m compileall -q scripts/wait_for_dependencies.py tests/test_wait_for_dependencies.py
git diff --check -- scripts/wait_for_dependencies.py tests/test_wait_for_dependencies.py
```

Expected: both commands exit `0` with no output.

- [ ] **Step 6: Commit only the readiness gate**

Run:

```powershell
git add -- scripts/wait_for_dependencies.py tests/test_wait_for_dependencies.py
git diff --cached --name-status
git commit -m "feat: add bounded dependency readiness gate"
```

Expected staged paths before commit:

```text
A scripts/wait_for_dependencies.py
A tests/test_wait_for_dependencies.py
```

---

### Task 2: Add the safe Qdrant collection provisioning command

**Files:**
- Create: `tests/test_qdrant_provisioning.py`
- Create: `scripts/ensure_shared_guide_collection.py`
- Verify unchanged behavior: `app/rag/qdrant_index.py`

**Interfaces:**
- Consumes: the existing `settings.QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_TIMEOUT_SECONDS`, `QDRANT_COLLECTION`, and `EMBEDDING_DIMENSION` values.
- Produces: `validate_collection_name(value: str) -> str`, `provision_collection(...) -> str`, and `main() -> int`.
- Delegates create/validate/index creation to `QdrantSharedGuideIndex.ensure_collection()`; the command itself has no delete path.

- [ ] **Step 1: Write the failing provisioning tests**

Create `tests/test_qdrant_provisioning.py`:

```python
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ensure_shared_guide_collection as provisioning


class FakeIndex:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.ensure_calls = 0

    def ensure_collection(self) -> None:
        self.ensure_calls += 1


def make_settings(**overrides):
    values = {
        "QDRANT_URL": "http://qdrant:6333",
        "QDRANT_API_KEY": "",
        "QDRANT_TIMEOUT_SECONDS": 5,
        "QDRANT_COLLECTION": "shared_guide_embeddings_v1",
        "EMBEDDING_DIMENSION": 768,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class QdrantProvisioningTests(unittest.TestCase):
    def test_provisioning_wires_v1_index_and_calls_ensure_once(self) -> None:
        client = object()
        observed = {}

        def client_factory(**kwargs):
            observed["client_kwargs"] = kwargs
            return client

        def index_factory(**kwargs):
            index = FakeIndex(**kwargs)
            observed["index"] = index
            return index

        collection = provisioning.provision_collection(
            settings_obj=make_settings(),
            client_factory=client_factory,
            index_factory=index_factory,
        )

        self.assertEqual("shared_guide_embeddings_v1", collection)
        self.assertEqual(
            {
                "url": "http://qdrant:6333",
                "api_key": "",
                "timeout_seconds": 5.0,
            },
            observed["client_kwargs"],
        )
        self.assertEqual(
            {
                "client": client,
                "collection": "shared_guide_embeddings_v1",
                "dimension": 768,
            },
            observed["index"].kwargs,
        )
        self.assertEqual(1, observed["index"].ensure_calls)

    def test_rejects_non_versioned_collection_before_client_creation(self) -> None:
        calls = []

        with self.assertRaises(ValueError):
            provisioning.provision_collection(
                settings_obj=make_settings(QDRANT_COLLECTION="default"),
                client_factory=lambda **kwargs: calls.append(kwargs),
                index_factory=FakeIndex,
            )

        self.assertEqual([], calls)

    def test_rejects_non_v1_dimension_before_client_creation(self) -> None:
        calls = []

        with self.assertRaises(ValueError):
            provisioning.provision_collection(
                settings_obj=make_settings(EMBEDDING_DIMENSION=1536),
                client_factory=lambda **kwargs: calls.append(kwargs),
                index_factory=FakeIndex,
            )

        self.assertEqual([], calls)

    def test_main_reports_only_error_class(self) -> None:
        private_error = "sk-private-value https://user:password@qdrant/private"
        errors = io.StringIO()

        with patch.object(
            provisioning,
            "provision_collection",
            side_effect=RuntimeError(private_error),
        ), redirect_stderr(errors):
            exit_code = provisioning.main()

        self.assertEqual(1, exit_code)
        self.assertIn(
            "qdrant_collection status=failed error_class=RuntimeError",
            errors.getvalue(),
        )
        self.assertNotIn(private_error, errors.getvalue())
        self.assertNotIn("password", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the provisioning tests and verify they fail**

Run:

```powershell
python -m unittest discover -s tests -p "test_qdrant_provisioning.py" -v
```

Expected: FAIL because `scripts.ensure_shared_guide_collection` does not exist.

- [ ] **Step 3: Implement idempotent provisioning through the existing adapter**

Create `scripts/ensure_shared_guide_collection.py`:

```python
"""Create or validate the configured shared-guide Qdrant collection safely."""

from __future__ import annotations

import math
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import settings
from app.rag.qdrant_index import QdrantSharedGuideIndex, create_qdrant_client


_COLLECTION_NAME = re.compile(r"^shared_guide_embeddings_v[1-9][0-9]*$")
_V1_DIMENSION = 768


def validate_collection_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not _COLLECTION_NAME.fullmatch(normalized):
        raise ValueError("QDRANT_COLLECTION must be an explicit shared-guide version")
    return normalized


def _timeout(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("QDRANT_TIMEOUT_SECONDS must be positive")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("QDRANT_TIMEOUT_SECONDS must be positive")
    return parsed


def provision_collection(
    *,
    settings_obj: Any = settings,
    client_factory: Callable[..., Any] = create_qdrant_client,
    index_factory: Callable[..., Any] = QdrantSharedGuideIndex,
) -> str:
    collection = validate_collection_name(settings_obj.QDRANT_COLLECTION)
    dimension = settings_obj.EMBEDDING_DIMENSION
    if isinstance(dimension, bool) or dimension != _V1_DIMENSION:
        raise ValueError("shared-guide V1 requires EMBEDDING_DIMENSION=768")
    url = str(settings_obj.QDRANT_URL or "").strip()
    if not url:
        raise ValueError("QDRANT_URL is required")
    client = client_factory(
        url=url,
        api_key=settings_obj.QDRANT_API_KEY,
        timeout_seconds=_timeout(settings_obj.QDRANT_TIMEOUT_SECONDS),
    )
    index = index_factory(
        client=client,
        collection=collection,
        dimension=_V1_DIMENSION,
    )
    index.ensure_collection()
    return collection


def main() -> int:
    try:
        collection = provision_collection()
    except Exception as error:
        print(
            f"qdrant_collection status=failed error_class={type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print(
        f"qdrant_collection status=ready collection={collection} "
        "dimension=768 distance=Cosine"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the new and existing Qdrant safety tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_qdrant_provisioning.py" -v
python -m unittest discover -s tests -p "test_qdrant_index.py" -v
```

Expected: provisioning tests PASS; existing Qdrant adapter tests PASS, including mismatched-schema rejection and the assertion that no collection deletion occurs.

- [ ] **Step 5: Check syntax, whitespace, and sensitive-output patterns**

Run:

```powershell
python -m compileall -q scripts/ensure_shared_guide_collection.py tests/test_qdrant_provisioning.py
git diff --check -- scripts/ensure_shared_guide_collection.py tests/test_qdrant_provisioning.py
rg -n "print\(.*(QDRANT_URL|QDRANT_API_KEY|error\))" scripts/ensure_shared_guide_collection.py
```

Expected: compile and diff checks exit `0`; `rg` returns no matches.

- [ ] **Step 6: Commit only the provisioning command**

Run:

```powershell
git add -- scripts/ensure_shared_guide_collection.py tests/test_qdrant_provisioning.py
git diff --cached --name-status
git commit -m "feat: add qdrant collection provisioning command"
```

Expected staged paths before commit:

```text
A scripts/ensure_shared_guide_collection.py
A tests/test_qdrant_provisioning.py
```

---

### Task 3: Add the reproducible backend image and ignore boundaries

**Files:**
- Create: `tests/test_container_image_contract.py`
- Create: `Dockerfile`
- Create: `.dockerignore`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: repository source and `requirements.txt` only at build time; runtime configuration comes only from Compose `env_file`.
- Produces: image command `python scripts/wait_for_dependencies.py && exec python -m uvicorn main:app --host 0.0.0.0 --port 8000`.
- Image includes: `requirements.txt`, `alembic.ini`, `main.py`, `app/`, `migrations/`, and `scripts/`.

- [ ] **Step 1: Write the failing container contract tests**

Create `tests/test_container_image_contract.py`:

```python
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_IMAGE = (
    "ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm@"
    "sha256:19a8c92b461bbc32e8bd30c15132cec1d16c49c61f4359c9225262938485f513"
)


class ContainerImageContractTests(unittest.TestCase):
    def test_dockerfile_uses_pinned_ssl_capable_runtime(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn(f"FROM {PINNED_IMAGE}", dockerfile)
        self.assertIn("uv pip install --system --no-cache -r requirements.txt", dockerfile)
        self.assertIn("ssl.OPENSSL_VERSION.startswith('OpenSSL 3.')", dockerfile)

    def test_dockerfile_copies_only_the_required_runtime_inputs(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        copy_lines = {
            line.strip()
            for line in dockerfile.splitlines()
            if line.strip().startswith("COPY ")
        }

        self.assertEqual(
            {
                "COPY requirements.txt requirements.txt",
                "COPY alembic.ini alembic.ini",
                "COPY main.py main.py",
                "COPY app app",
                "COPY migrations migrations",
                "COPY scripts scripts",
            },
            copy_lines,
        )
        self.assertIsNone(re.search(r"(?m)^COPY\s+\.\s", dockerfile))

    def test_dockerfile_waits_before_uvicorn_and_compiles_sources(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("python -m compileall -q app main.py scripts migrations", dockerfile)
        self.assertIn(
            'CMD ["sh", "-c", "python scripts/wait_for_dependencies.py '
            '&& exec python -m uvicorn main:app --host 0.0.0.0 --port 8000"]',
            dockerfile,
        )

    def test_docker_context_excludes_credentials_and_local_state(self) -> None:
        patterns = {
            line.strip()
            for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        for required in {
            ".git/",
            ".env*",
            ".venv/",
            "__pycache__/",
            "tests/",
            "docs/",
            "data/",
            "*.log",
            ".idea/",
            ".vscode/",
            "docker-compose*.yml",
            "compose*.yaml",
        }:
            self.assertIn(required, patterns)

    def test_git_ignores_server_only_service_configuration(self) -> None:
        patterns = {
            line.strip()
            for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        for required in {
            "compose.yaml",
            "docker-compose*.yml",
            "infra.env",
            "backend.env",
        }:
            self.assertIn(required, patterns)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the container contract tests and verify they fail**

Run:

```powershell
python -m unittest discover -s tests -p "test_container_image_contract.py" -v
```

Expected: FAIL because `Dockerfile` and `.dockerignore` do not exist and the server-only ignore entries are absent.

- [ ] **Step 3: Add the pinned Dockerfile**

Create `Dockerfile`:

```dockerfile
FROM ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm@sha256:19a8c92b461bbc32e8bd30c15132cec1d16c49c61f4359c9225262938485f513

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN python -c "import ssl; assert ssl.OPENSSL_VERSION.startswith('OpenSSL 3.')"

COPY requirements.txt requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

COPY alembic.ini alembic.ini
COPY main.py main.py
COPY app app
COPY migrations migrations
COPY scripts scripts

RUN python -m compileall -q app main.py scripts migrations

EXPOSE 8000

CMD ["sh", "-c", "python scripts/wait_for_dependencies.py && exec python -m uvicorn main:app --host 0.0.0.0 --port 8000"]
```

- [ ] **Step 4: Add the Docker build-context exclusions**

Create `.dockerignore`:

```text
# Version control and agent metadata
.git/
.gitattributes
.agents/
.codex/
.superpowers/

# Runtime credentials and local configuration
.env*
infra.env
backend.env
docker-compose*.yml
compose*.yaml

# Python environments and generated files
.venv/
venv/
env/
__pycache__/
*.py[cod]
*.so
*.egg-info/
build/
dist/

# Tests, coverage, documentation, and local runtime data
tests/
docs/
.pytest_cache/
.coverage
htmlcov/
data/
*.db
*.log
logs/
e2e-*.json

# Editors and operating systems
.idea/
.vscode/
*.swp
*.swo
*~
.DS_Store
Thumbs.db
```

- [ ] **Step 5: Protect server-only files from accidental Git staging**

Append this exact block to `.gitignore`:

```gitignore

# --- Server-only deployment configuration ---
compose.yaml
docker-compose*.yml
infra.env
backend.env
```

Do not delete or rewrite the existing ignore rules.

- [ ] **Step 6: Run the container contract and syntax checks**

Run:

```powershell
python -m unittest discover -s tests -p "test_container_image_contract.py" -v
python -m compileall -q app main.py scripts migrations
git diff --check -- Dockerfile .dockerignore .gitignore tests/test_container_image_contract.py
```

Expected: five tests PASS; compile and diff checks exit `0` with no output.

- [ ] **Step 7: Confirm local service configuration is ignored but preserved**

Run:

```powershell
git check-ignore -v -- docker-compose.qdrant.yml
Test-Path -LiteralPath docker-compose.qdrant.yml
git status --short
```

Expected:

- `git check-ignore` identifies the new `.gitignore` rule.
- `Test-Path` returns `True`; the file was not deleted.
- `.env.example` remains modified but unstaged; `docker-compose.qdrant.yml` no longer appears as an untracked file.

- [ ] **Step 8: Commit only the generic image contract**

Run:

```powershell
git add -- Dockerfile .dockerignore .gitignore tests/test_container_image_contract.py
git diff --cached --name-status
git commit -m "build: add reproducible backend container"
```

Expected staged paths before commit:

```text
A .dockerignore
M .gitignore
A Dockerfile
A tests/test_container_image_contract.py
```

---

### Task 4: Add the server deployment and rollback runbook

**Files:**
- Modify: `docs/shared-guide-rag-operations.md`

**Interfaces:**
- Consumes: the repository image contract from Tasks 1–3 and the existing server Compose project.
- Produces: exact operator commands for server-only configuration, migration, provisioning, health checks, staged feature enablement, diagnosis, and rollback.

- [ ] **Step 1: Verify the deployment section is absent**

Run:

```powershell
rg -n "CentOS 7 Compose deployment|ensure_shared_guide_collection.py|force-recreate backend" docs/shared-guide-rag-operations.md
```

Expected: no match for `CentOS 7 Compose deployment` or `ensure_shared_guide_collection.py`.

- [ ] **Step 2: Add the exact deployment section after “Local setup and readiness”**

Insert the following section into `docs/shared-guide-rag-operations.md` after the local stop command and before the production Qdrant paragraph:

````markdown
## CentOS 7 Compose deployment

The production host keeps its active Compose and environment files outside this
repository in `/home/aicreator/apps/travel-agent-infra`. Do not copy those files
into the backend checkout or add them to Git. Keep `infra.env` and `backend.env`
at mode `0600`.

Add only this `backend` service to the existing `services:` mapping in the
server's `compose.yaml`; leave the existing MySQL/Qdrant services and named
volumes unchanged:

```yaml
  backend:
    build:
      context: ../travel-agent-backend
      dockerfile: Dockerfile
    image: travel-agent-backend:rag
    container_name: travel-agent-backend
    restart: unless-stopped
    env_file:
      - ./backend.env
    depends_on:
      mysql:
        condition: service_healthy
      qdrant:
        condition: service_started
    ports:
      - "127.0.0.1:8000:8000"
    mem_limit: 768m
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read(1)
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 30s
```

`backend.env` must retain the approved LLM, map-provider, authentication, and
other application settings and must include these deployment values:

```text
DATABASE_BACKEND=mysql
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_DATABASE=travel_agent
MYSQL_USER=travel_agent_app

QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=shared_guide_embeddings_v1
QDRANT_TIMEOUT_SECONDS=5

EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=768

REDIS_ENABLED=false
SHARE_SQUARE_ENABLED=false
RAG_ENABLED=false
DEPENDENCY_WAIT_TIMEOUT_SECONDS=60
```

Populate `MYSQL_PASSWORD`, `JWT_SECRET_KEY`, `DASHSCOPE_API_KEY`,
`DASHSCOPE_BASE_URL`, and the active LLM/map-provider credentials only in the
server file. `JWT_SECRET_KEY` must contain at least 32 characters. The current
host-local Qdrant deployment may keep `QDRANT_API_KEY` empty.

Validate, build, and start dependencies without printing resolved environment
values:

```bash
cd /home/aicreator/apps/travel-agent-infra
chmod 600 infra.env backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml config --quiet
sudo docker compose --env-file ./infra.env -f ./compose.yaml build backend
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d mysql qdrant
sudo docker compose --env-file ./infra.env -f ./compose.yaml ps
curl -fsS http://127.0.0.1:6333/readyz
```

Run migrations and provision Qdrant before starting the backend:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend alembic upgrade head
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend alembic current
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/ensure_shared_guide_collection.py
```

`alembic current` must report `f4c2a81d9e30 (head)`. Provisioning must report
`collection=shared_guide_embeddings_v1 dimension=768 distance=Cosine` and must
stop without mutation on an incompatible existing collection.

Start with both features disabled, then verify health and binding:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d backend
sudo docker compose --env-file ./infra.env -f ./compose.yaml ps
curl -fsS http://127.0.0.1:8000/api/health
sudo ss -lntp | grep -E '127\.0\.0\.1:8000\b'
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/reindex_shared_guides.py
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/reconcile_shared_guide_index.py
```

Enable sharing first by changing only `SHARE_SQUARE_ENABLED=true`, then recreate
only the backend so it receives the updated environment:

```bash
sed -i 's/^SHARE_SQUARE_ENABLED=.*/SHARE_SQUARE_ENABLED=true/' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS 'http://127.0.0.1:8000/api/shared-guides?limit=1'
```

Use an authenticated existing account and one owned completed trip session to
publish a guide through `POST /api/trip/sessions/{session_id}/share`; wait until
its owned projection reports `publication_status=PUBLIC` and
`index_status=READY`, then confirm it appears in the public list.

Enable RAG only after that publication is indexed:

```bash
sed -i 's/^RAG_ENABLED=.*/RAG_ENABLED=true/' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
```

The health response must report Qdrant and RAG ready with
`embedding_configured=true`. Generate one same-city trip and confirm retrieval
uses the indexed public guide through the RAG metrics/response metadata.

After first-deployment acceptance, normal host startup uses:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d
```

Continue to use `up -d --force-recreate backend` whenever `backend.env`
changes; a plain `restart backend` can retain the old container environment.

For rollback, disable both feature flags and recreate only the backend:

```bash
sed -i 's/^RAG_ENABLED=.*/RAG_ENABLED=false/' backend.env
sed -i 's/^SHARE_SQUARE_ENABLED=.*/SHARE_SQUARE_ENABLED=false/' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
```

Keep the MySQL revision, Qdrant collection, and named volumes. If code rollback
is also required, check out the previous approved commit, rebuild only
`backend`, and recreate only that service. Do not run `alembic downgrade`, and
never use `docker compose down -v` as application rollback.
````

- [ ] **Step 3: Verify that the runbook contains every staged deployment command**

Run:

```powershell
rg -n "CentOS 7 Compose deployment|config --quiet|build backend|alembic upgrade head|f4c2a81d9e30|ensure_shared_guide_collection.py|127\.0\.0\.1:8000:8000|SHARE_SQUARE_ENABLED=true|RAG_ENABLED=true|force-recreate backend|down -v" docs/shared-guide-rag-operations.md
git diff --check -- docs/shared-guide-rag-operations.md
```

Expected: every required phrase has at least one match and the diff check exits `0`.

- [ ] **Step 4: Commit only the runbook**

Run:

```powershell
git add -- docs/shared-guide-rag-operations.md
git diff --cached --name-status
git commit -m "docs: add backend compose deployment runbook"
```

Expected staged path before commit:

```text
M docs/shared-guide-rag-operations.md
```

---

### Task 5: Run the bounded local implementation gate

**Files:**
- Verify: all files changed in Tasks 1–4
- Preserve unstaged: `.env.example`
- Preserve untracked-but-now-ignored: `docker-compose.qdrant.yml`

**Interfaces:**
- Consumes: the four independently committed repository deliverables.
- Produces: evidence that targeted tests, syntax checks, ignore boundaries, and secret scans pass without running the full suite.

- [ ] **Step 1: Run all targeted deployment tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_wait_for_dependencies.py" -v
python -m unittest discover -s tests -p "test_qdrant_provisioning.py" -v
python -m unittest discover -s tests -p "test_container_image_contract.py" -v
python -m unittest discover -s tests -p "test_qdrant_index.py" -v
```

Expected: all selected tests PASS. Do not replace this with an unbounded `python -m unittest discover` invocation.

- [ ] **Step 2: Compile the exact runtime inputs copied into the image**

Run:

```powershell
python -m compileall -q app main.py scripts migrations
```

Expected: exit `0` with no output.

- [ ] **Step 3: Verify formatting and forbidden secret assignments**

Run:

```powershell
git diff --check c319ac5..HEAD
rg -n "(sk-[A-Za-z0-9._-]{20,}|MYSQL_PASSWORD=\S+|JWT_SECRET_KEY=\S+|DASHSCOPE_API_KEY=\S+|QDRANT_API_KEY=\S+)" Dockerfile .dockerignore .gitignore scripts/wait_for_dependencies.py scripts/ensure_shared_guide_collection.py tests/test_wait_for_dependencies.py tests/test_qdrant_provisioning.py tests/test_container_image_contract.py docs/shared-guide-rag-operations.md
```

Expected: the diff check exits `0`; the sensitive-value scan returns no matches. Variable names with blank values or prose references are acceptable, but no value may be embedded.

- [ ] **Step 4: Prove local service configuration was excluded from implementation commits**

Run:

```powershell
git diff --name-only c319ac5..HEAD -- .env.example docker-compose.qdrant.yml
git ls-files --error-unmatch docker-compose.qdrant.yml
git status -sb
```

Expected:

- The first command prints nothing.
- `git ls-files` exits nonzero because `docker-compose.qdrant.yml` is not tracked.
- `.env.example` remains a user-owned local modification and is not staged.

- [ ] **Step 5: Review the implementation commit boundary**

Run:

```powershell
git log --oneline --decorate -5
git diff --stat c319ac5..HEAD
git status --short
```

Expected: the plan commit and four implementation commits follow `c319ac5`; no implementation file remains unstaged, and the only visible dirty path is the pre-existing `.env.example` modification.

---

### Task 6: Prepare the server-only backend service

**Files:**
- Modify only on server: `/home/aicreator/apps/travel-agent-infra/compose.yaml`
- Create or update only on server: `/home/aicreator/apps/travel-agent-infra/backend.env`
- Preserve only on server: `/home/aicreator/apps/travel-agent-infra/infra.env`

**Interfaces:**
- Consumes: a user-approved remote `rag` branch containing Tasks 1–5.
- Produces: a validated Compose model and locally built `travel-agent-backend:rag` image; no service is exposed beyond loopback.

- [ ] **Step 1: Update the server checkout only after the implementation commits are pushed with approval**

Run on the server:

```bash
cd /home/aicreator/apps/travel-agent-backend
git fetch origin
git checkout rag
git pull --ff-only origin rag
git status -sb
```

Expected: the checkout is on `rag`, contains `Dockerfile` and both new scripts, and has no server environment file in Git status.

- [ ] **Step 2: Add the backend service to the existing server Compose file**

In `/home/aicreator/apps/travel-agent-infra/compose.yaml`, add this exact service beneath the existing `services:` mapping without changing the MySQL/Qdrant blocks or volume names:

```yaml
  backend:
    build:
      context: ../travel-agent-backend
      dockerfile: Dockerfile
    image: travel-agent-backend:rag
    container_name: travel-agent-backend
    restart: unless-stopped
    env_file:
      - ./backend.env
    depends_on:
      mysql:
        condition: service_healthy
      qdrant:
        condition: service_started
    ports:
      - "127.0.0.1:8000:8000"
    mem_limit: 768m
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read(1)
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 30s
```

- [ ] **Step 3: Set the server runtime environment with both features disabled**

In `/home/aicreator/apps/travel-agent-infra/backend.env`, preserve the approved active LLM, map-provider, and authentication values and set these exact non-secret values:

```text
DATABASE_BACKEND=mysql
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_DATABASE=travel_agent
MYSQL_USER=travel_agent_app
QDRANT_URL=http://qdrant:6333
QDRANT_COLLECTION=shared_guide_embeddings_v1
QDRANT_TIMEOUT_SECONDS=5
EMBEDDING_MODEL=qwen3.7-text-embedding
EMBEDDING_DIMENSION=768
REDIS_ENABLED=false
SHARE_SQUARE_ENABLED=false
RAG_ENABLED=false
DEPENDENCY_WAIT_TIMEOUT_SECONDS=60
```

The same file must contain the server's existing values for `MYSQL_PASSWORD`, `JWT_SECRET_KEY`, `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, and the active LLM/map-provider variables. Keep `QDRANT_API_KEY` empty for the current loopback/internal-network deployment.

- [ ] **Step 4: Lock permissions and validate Compose without printing interpolation results**

Run:

```bash
cd /home/aicreator/apps/travel-agent-infra
chmod 600 infra.env backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml config --quiet
```

Expected: exit `0` and no rendered configuration or secret values in output.

- [ ] **Step 5: Build and inspect the backend image runtime**

Run:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml build backend
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm --no-deps backend python -c "import sys,ssl,fastapi,sqlalchemy,qdrant_client,pymysql; print(sys.version.split()[0]); print(ssl.OPENSSL_VERSION)"
```

Expected: build succeeds; output reports Python `3.12.x`, OpenSSL `3.x`, and no import error.

- [ ] **Step 6: Confirm the host port is not yet published**

Run:

```bash
sudo ss -lntp | grep -E ':8000\b'
```

Expected before backend startup: no match.

---

### Task 7: Migrate MySQL, provision Qdrant, and start with features disabled

**Files:**
- Read on server: `/home/aicreator/apps/travel-agent-infra/compose.yaml`
- Read on server: `/home/aicreator/apps/travel-agent-infra/backend.env`
- Persistent data: existing MySQL and Qdrant named volumes, unchanged

**Interfaces:**
- Consumes: healthy MySQL/Qdrant services and the built backend image.
- Produces: MySQL at Alembic head, a validated V1 Qdrant collection, and a healthy loopback-only backend with sharing/RAG disabled.

- [ ] **Step 1: Start and verify only the dependencies**

Run:

```bash
cd /home/aicreator/apps/travel-agent-infra
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d mysql qdrant
sudo docker compose --env-file ./infra.env -f ./compose.yaml ps
curl -fsS http://127.0.0.1:6333/readyz
```

Expected: MySQL is `healthy`; Qdrant is `Up`; readiness prints `all shards are ready`.

- [ ] **Step 2: Apply and verify the one-off Alembic migration**

Run:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend alembic upgrade head
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend alembic current
```

Expected: both commands exit `0`; current revision includes `f4c2a81d9e30 (head)`. If migration fails, stop this task and do not start the backend.

- [ ] **Step 3: Create or validate the Qdrant collection explicitly**

Run:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/ensure_shared_guide_collection.py
```

Expected:

```text
qdrant_collection status=ready collection=shared_guide_embeddings_v1 dimension=768 distance=Cosine
```

If the command reports `QdrantSchemaMismatchError`, stop without deleting or recreating the collection.

- [ ] **Step 4: Start the backend and wait for container health**

Run:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d backend
sudo docker compose --env-file ./infra.env -f ./compose.yaml ps
sudo docker compose --env-file ./infra.env -f ./compose.yaml logs --tail=100 backend
```

Expected: `travel-agent-backend` transitions to `healthy`; logs show sanitized dependency readiness and no URL credentials, API keys, passwords, prompts, or provider response bodies.

- [ ] **Step 5: Verify API health and loopback-only publication**

Run:

```bash
curl -fsS http://127.0.0.1:8000/api/health
sudo ss -lntp | grep -E '127\.0\.0\.1:8000\b'
sudo ss -lntp | grep -E '0\.0\.0\.0:8000|\[::\]:8000'
```

Expected: health returns HTTP 200; the first listener check matches `127.0.0.1:8000`; the wildcard-listener check returns no match.

- [ ] **Step 6: Run maintenance commands in dry-run mode**

Run:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/reindex_shared_guides.py
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/reconcile_shared_guide_index.py
```

Expected: both exit `0`, validate the collection schema, avoid DashScope calls and writes, and report `schema_mismatch=False` with `failed=0`.

---

### Task 8: Enable sharing, then RAG, with rollback evidence

**Files:**
- Modify only on server: `/home/aicreator/apps/travel-agent-infra/backend.env`
- Preserve: MySQL rows, Alembic revision, Qdrant points, and all named volumes

**Interfaces:**
- Consumes: a healthy disabled-feature deployment and an authenticated existing user with an owned completed trip session.
- Produces: one public indexed guide, ready RAG health, one retrieval-backed generation, and a tested non-destructive feature rollback command.

- [ ] **Step 1: Enable sharing only and recreate the backend**

Run:

```bash
cd /home/aicreator/apps/travel-agent-infra
sed -i 's/^SHARE_SQUARE_ENABLED=.*/SHARE_SQUARE_ENABLED=true/' backend.env
grep -q '^SHARE_SQUARE_ENABLED=true$' backend.env
grep -q '^RAG_ENABLED=false$' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
```

Expected: commands exit `0`; sharing is enabled while RAG remains disabled; backend returns HTTP 200 after recreation.

- [ ] **Step 2: Publish and verify one real guide**

Using an existing authenticated account in the frontend or API client:

1. Select one owned completed trip session.
2. Call `POST /api/trip/sessions/{session_id}/share` with JSON `{"title":"部署验收攻略"}`.
3. Poll `GET /api/users/me/shared-guides?limit=20` with the same bearer token.
4. Require the new item to reach `publication_status=PUBLIC` and `index_status=READY`.
5. Call `GET /api/shared-guides?limit=20` without authentication and require the same `share_id` to appear.

Expected: publication succeeds, the worker embeds with `qwen3.7-text-embedding`, Qdrant indexing completes, and public read exposes no private retrieval/internal fields.

- [ ] **Step 3: Reconcile the now-populated collection**

Run:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml run --rm backend python scripts/reconcile_shared_guide_index.py
```

Expected: exit `0`, no missing/stale/schema-mismatch failures for the published guide.

- [ ] **Step 4: Enable RAG and recreate only the backend**

Run:

```bash
sed -i 's/^RAG_ENABLED=.*/RAG_ENABLED=true/' backend.env
grep -q '^SHARE_SQUARE_ENABLED=true$' backend.env
grep -q '^RAG_ENABLED=true$' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
```

Expected: health reports Qdrant and RAG ready and `embedding_configured=true`.

- [ ] **Step 5: Verify one retrieval-backed generation**

Create a new trip request for the same city and compatible day/transport conditions as the published guide. Require ordinary guide generation to succeed and inspect the existing RAG metrics/response metadata for at least one accepted retrieval hit referencing that public guide. Confirm logs contain only sanitized counts/states, not retrieval text, prompts, API keys, or provider bodies.

- [ ] **Step 6: Record the non-destructive rollback commands without deleting data**

The tested rollback sequence is:

```bash
sed -i 's/^RAG_ENABLED=.*/RAG_ENABLED=false/' backend.env
sed -i 's/^SHARE_SQUARE_ENABLED=.*/SHARE_SQUARE_ENABLED=false/' backend.env
sudo docker compose --env-file ./infra.env -f ./compose.yaml up -d --force-recreate backend
curl -fsS http://127.0.0.1:8000/api/health
```

Expected when rollback is needed: ordinary generation remains available, sharing/RAG report disabled, and MySQL/Qdrant data and named volumes remain untouched. Do not execute rollback after a successful rollout unless rollback behavior itself has been requested.

- [ ] **Step 7: Capture final bounded deployment evidence**

Run:

```bash
sudo docker compose --env-file ./infra.env -f ./compose.yaml ps
sudo docker compose --env-file ./infra.env -f ./compose.yaml logs --tail=100 backend
sudo ss -lntp | grep -E '127\.0\.0\.1:(3306|6333|8000)\b'
```

Expected: MySQL, Qdrant, and backend are Up/healthy as applicable; all three published host ports are loopback-only; recent backend logs contain no secrets or full retrieval/application data.
