from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qsubagent.config import AppSettings


class ConfigTests(unittest.TestCase):
    def write_config(self, directory: str, content: str) -> Path:
        path = Path(directory) / "config.yaml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_api_key_comes_from_named_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "model:\n"
                "  name: test\n"
                "  base_url: ''\n"
                "  api_key_env: QSUBAGENT_TEST_KEY\n",
            )
            with patch.dict(os.environ, {"QSUBAGENT_TEST_KEY": "secret"}):
                settings = AppSettings.load(path)

            self.assertEqual(settings.model.api_key, "secret")

    def test_api_key_can_come_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "model:\n"
                "  name: test\n"
                "  base_url: ''\n"
                "  api_key: config-secret\n"
                "  api_key_env: QSUBAGENT_TEST_KEY\n",
            )
            with patch.dict(
                os.environ,
                {"QSUBAGENT_TEST_KEY": "environment-secret"},
            ):
                settings = AppSettings.load(path)

            self.assertEqual(settings.model.api_key, "config-secret")

    def test_api_key_in_config_must_be_a_string(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "model:\n  name: test\n  api_key: 123\n",
            )

            with self.assertRaisesRegex(ValueError, "model.api_key"):
                AppSettings.load(path)

    def test_string_false_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "model:\n  name: test\n"
                "capabilities:\n  previous_response_id: 'false'\n",
            )

            with self.assertRaisesRegex(ValueError, "YAML 布尔值"):
                AppSettings.load(path)

    def test_cli_execution_mode_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "model:\n  name: test\n"
                "powershell:\n  mode: approval\n",
            )

            settings = AppSettings.load(path, execution_mode="unrestricted")

            self.assertEqual(settings.powershell.mode, "unrestricted")

    def test_invalid_budget_relationship_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "model:\n  name: test\n"
                "runtime:\n  soft_tool_rounds: 4\n  hard_tool_rounds: 3\n",
            )

            with self.assertRaisesRegex(ValueError, "hard_tool_rounds"):
                AppSettings.load(path)
