"""本地 MySQL 应用账号配置脚本的无数据库单元测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from dotenv import load_dotenv

from scripts.configure_mysql_app_user import (
    _generate_password,
    _replace_env_value,
    _require_identifier,
)


class MySQLAppUserConfigTests(unittest.TestCase):
    def test_generated_password_is_high_entropy_and_dotenv_safe(self) -> None:
        password = _generate_password()

        self.assertEqual(len(password), 40)
        self.assertNotIn("'", password)
        self.assertNotIn('"', password)
        self.assertNotIn("\\", password)
        self.assertNotIn("#", password)
        self.assertNotIn("$", password)
        self.assertFalse(any(character.isspace() for character in password))

    def test_replace_env_value_updates_existing_key_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "DATABASE_BACKEND=sqlite\nMYSQL_PASSWORD=old\nMYSQL_HOST=127.0.0.1\n",
                encoding="utf-8",
            )

            _replace_env_value(path, "MYSQL_PASSWORD", "new-secret")

            content = path.read_text(encoding="utf-8")
            self.assertIn("MYSQL_PASSWORD=new-secret\n", content)
            self.assertEqual(content.count("MYSQL_PASSWORD="), 1)
            self.assertIn("DATABASE_BACKEND=sqlite", content)
            self.assertIn("MYSQL_HOST=127.0.0.1", content)

    def test_replace_env_value_appends_missing_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DATABASE_BACKEND=sqlite\n", encoding="utf-8")

            _replace_env_value(path, "MYSQL_PASSWORD", "generated")

            self.assertTrue(
                path.read_text(encoding="utf-8").endswith(
                    "\nMYSQL_PASSWORD=generated\n"
                )
            )

    def test_local_env_overrides_stale_parent_process_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_env = Path(directory) / ".env"
            local_env = Path(directory) / ".env.local"
            base_env.write_text("MYSQL_PASSWORD=stale-base\n", encoding="utf-8")
            local_env.write_text("MYSQL_PASSWORD=current-local\n", encoding="utf-8")

            with patch.dict(os.environ, {"MYSQL_PASSWORD": "stale-parent"}, clear=False):
                load_dotenv(base_env, override=False)
                load_dotenv(local_env, override=True)
                self.assertEqual(os.environ["MYSQL_PASSWORD"], "current-local")

    def test_identifier_validation_rejects_sql_fragment(self) -> None:
        self.assertEqual(_require_identifier("travel_agent", "数据库"), "travel_agent")
        with self.assertRaises(ValueError):
            _require_identifier("travel_agent; DROP DATABASE mysql", "数据库")


if __name__ == "__main__":
    unittest.main()
