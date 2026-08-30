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
