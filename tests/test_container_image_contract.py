from __future__ import annotations

import fnmatch
import json
import shlex
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PINNED_IMAGE = (
    "ghcr.io/astral-sh/uv:0.9.5-python3.12-bookworm@"
    "sha256:19a8c92b461bbc32e8bd30c15132cec1d16c49c61f4359c9225262938485f513"
)


@dataclass(frozen=True)
class DockerInstruction:
    keyword: str
    arguments: str


@dataclass(frozen=True)
class IgnoreRule:
    pattern: str
    negated: bool


def parse_dockerfile(text: str) -> list[DockerInstruction]:
    instructions = []
    logical_line = ""

    def append_instruction(line: str) -> None:
        keyword, separator, arguments = line.partition(" ")
        instructions.append(
            DockerInstruction(
                keyword=keyword.upper(),
                arguments=arguments.strip() if separator else "",
            )
        )

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        logical_line = f"{logical_line} {line}".strip() if logical_line else line
        if logical_line.endswith("\\"):
            logical_line = logical_line[:-1].rstrip()
            continue
        append_instruction(logical_line)
        logical_line = ""

    if logical_line:
        append_instruction(logical_line)
    return instructions


def parse_ignore_rules(text: str) -> list[IgnoreRule]:
    rules = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        if pattern:
            rules.append(IgnoreRule(pattern=pattern, negated=negated))
    return rules


def _ignore_rule_matches(rule: IgnoreRule, path: str) -> bool:
    normalized_path = path.replace("\\", "/").lstrip("/")
    normalized_pattern = rule.pattern.replace("\\", "/").lstrip("/")
    path_without_trailing_slash = normalized_path.rstrip("/")
    pattern_without_trailing_slash = normalized_pattern.rstrip("/")
    if "/" not in pattern_without_trailing_slash:
        basename = path_without_trailing_slash.rsplit("/", 1)[-1]
        return fnmatch.fnmatchcase(
            path_without_trailing_slash,
            pattern_without_trailing_slash,
        ) or fnmatch.fnmatchcase(basename, pattern_without_trailing_slash)
    return fnmatch.fnmatchcase(
        normalized_path,
        normalized_pattern,
    ) or fnmatch.fnmatchcase(
        path_without_trailing_slash,
        pattern_without_trailing_slash,
    )


def is_ignored(path: str, rules: list[IgnoreRule]) -> bool:
    ignored = False
    for rule in rules:
        if _ignore_rule_matches(rule, path):
            ignored = not rule.negated
    return ignored


class ContainerImageContractTests(unittest.TestCase):
    def test_ignore_rules_use_the_last_matching_rule(self) -> None:
        rules = parse_ignore_rules(
            "*.env\ncompose.yaml\n!backend.env\n!compose.yaml\n"
        )

        self.assertTrue(is_ignored("infra.env", rules))
        self.assertFalse(is_ignored("backend.env", rules))
        self.assertFalse(is_ignored("compose.yaml", rules))

    def test_dockerfile_parser_ignores_comments_and_joins_continuations(self) -> None:
        instructions = parse_dockerfile(
            """
            # comment
            ENV FIRST=one \\
                SECOND=two

            RUN echo ready
            """
        )

        self.assertEqual(
            [("ENV", "FIRST=one SECOND=two"), ("RUN", "echo ready")],
            [(item.keyword, item.arguments) for item in instructions],
        )

    def test_dockerfile_uses_pinned_ssl_capable_runtime(self) -> None:
        instructions = parse_dockerfile(
            (ROOT / "Dockerfile").read_text(encoding="utf-8")
        )

        from_arguments = [
            item.arguments for item in instructions if item.keyword == "FROM"
        ]
        self.assertEqual([PINNED_IMAGE], from_arguments)

        run_arguments = [
            item.arguments for item in instructions if item.keyword == "RUN"
        ]
        self.assertEqual(
            1,
            run_arguments.count("uv pip install --system --no-cache -r requirements.txt"),
        )
        openssl_runs = [
            arguments
            for arguments in run_arguments
            if "ssl.OPENSSL_VERSION.startswith('OpenSSL 3.')" in arguments
        ]
        self.assertEqual(1, len(openssl_runs))
        self.assertEqual(
            [
                "python",
                "-c",
                "import ssl; assert ssl.OPENSSL_VERSION.startswith('OpenSSL 3.')",
            ],
            shlex.split(openssl_runs[0]),
        )

    def test_dockerfile_copies_only_the_required_runtime_inputs(self) -> None:
        instructions = parse_dockerfile(
            (ROOT / "Dockerfile").read_text(encoding="utf-8")
        )
        copy_arguments = [
            item.arguments for item in instructions if item.keyword == "COPY"
        ]
        self.assertEqual(
            {
                ("requirements.txt", "requirements.txt"),
                ("alembic.ini", "alembic.ini"),
                ("main.py", "main.py"),
                ("app", "app"),
                ("migrations", "migrations"),
                ("scripts", "scripts"),
            },
            {tuple(shlex.split(arguments)) for arguments in copy_arguments},
        )
        self.assertEqual(6, len(copy_arguments))

    def test_dockerfile_waits_before_uvicorn_and_compiles_sources(self) -> None:
        instructions = parse_dockerfile(
            (ROOT / "Dockerfile").read_text(encoding="utf-8")
        )
        run_arguments = [
            item.arguments for item in instructions if item.keyword == "RUN"
        ]
        self.assertEqual(
            1,
            run_arguments.count("python -m compileall -q app main.py scripts migrations"),
        )

        cmd_arguments = [
            item.arguments for item in instructions if item.keyword == "CMD"
        ]
        self.assertEqual(1, len(cmd_arguments))
        self.assertEqual(
            [
                "sh",
                "-c",
                "python scripts/wait_for_dependencies.py && exec python -m uvicorn main:app --host 0.0.0.0 --port 8000",
            ],
            json.loads(cmd_arguments[0]),
        )

    def test_docker_context_excludes_credentials_and_local_state(self) -> None:
        rules = parse_ignore_rules(
            (ROOT / ".dockerignore").read_text(encoding="utf-8")
        )

        for required in {
            ".git/",
            ".env.example",
            ".venv/",
            "__pycache__/",
            "tests/",
            "docs/",
            "data/",
            "application.log",
            "backend.env",
            "infra.env",
            ".idea/",
            ".vscode/",
            "docker-compose.qdrant.yml",
            "compose.yaml",
        }:
            self.assertTrue(is_ignored(required, rules), required)

    def test_git_ignores_server_only_service_configuration(self) -> None:
        rules = parse_ignore_rules(
            (ROOT / ".gitignore").read_text(encoding="utf-8")
        )

        for required in {
            "compose.yaml",
            "docker-compose.qdrant.yml",
            "infra.env",
            "backend.env",
        }:
            self.assertTrue(is_ignored(required, rules), required)


if __name__ == "__main__":
    unittest.main()
