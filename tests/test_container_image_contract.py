from __future__ import annotations

import fnmatch
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class IgnoreRule:
    pattern: str
    negated: bool


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


class DeploymentIgnoreContractTests(unittest.TestCase):
    def test_ignore_rules_use_the_last_matching_rule(self) -> None:
        rules = parse_ignore_rules(
            "*.env\ncompose.yaml\n!backend.env\n!compose.yaml\n"
        )

        self.assertTrue(is_ignored("infra.env", rules))
        self.assertFalse(is_ignored("backend.env", rules))
        self.assertFalse(is_ignored("compose.yaml", rules))

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
