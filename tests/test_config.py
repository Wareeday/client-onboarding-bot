"""Unit tests for layered config loading (.env, YAML, JSON, TOML) and validation."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from client_onboarding_bot.config import load_env_file, load_settings, validate_settings  # noqa: E402


class TestConfig(unittest.TestCase):
    def test_builtin_env_parser_and_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                '# comment\n'
                'export TEST_ONBOARD_A="hello world"\n'
                "TEST_ONBOARD_B='quoted'#ignored\n"
                "TEST_ONBOARD_C=plain_val # trailing comment\n"
                "TEST_EXISTING=from_file\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"TEST_EXISTING": "already_set"}, clear=False):
                load_env_file(env_path)
                self.assertEqual(os.environ.get("TEST_ONBOARD_A"), "hello world")
                self.assertEqual(os.environ.get("TEST_ONBOARD_B"), "quoted")
                self.assertEqual(os.environ.get("TEST_ONBOARD_C"), "plain_val")
                self.assertEqual(os.environ.get("TEST_EXISTING"), "already_set")
                os.environ.pop("TEST_ONBOARD_A", None)
                os.environ.pop("TEST_ONBOARD_B", None)
                os.environ.pop("TEST_ONBOARD_C", None)

    def test_yaml_example_config_loads_cleanly(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        yaml_cfg = repo_root / "config.example.yaml"
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(config_file=yaml_cfg, output_dir=Path(tmp) / "out")
            self.assertEqual(settings.agency.agency_name, "Your Automation Studio")
            self.assertEqual(settings.retry.max_attempts, 3)
            self.assertEqual(len(settings.milestones), 4)
            self.assertEqual(validate_settings(settings), [])

    def test_toml_config_and_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            toml_path = Path(tmp) / "agency.toml"
            toml_path.write_text(
                '[agency]\n'
                'agency_name = "Vertex Labs"\n'
                'sender_email = "bad-email"\n'
                'tax_rate = 1.5\n'
                'invoice_due_days = 30\n',
                encoding="utf-8",
            )
            settings = load_settings(config_file=toml_path, output_dir=Path(tmp) / "out")
            self.assertEqual(settings.agency.agency_name, "Vertex Labs")
            self.assertEqual(settings.agency.tax_rate, Decimal("1.5"))

            errors = validate_settings(
                settings, mode="gdrive", send_emails=True, notify_webhook=True
            )
            joined = "\n".join(errors)
            self.assertIn("sender_email", joined)
            self.assertIn("tax_rate", joined)
            self.assertIn("credentials", joined)
            self.assertIn("SMTP", joined)
            self.assertIn("WEBHOOK_URL", joined)


if __name__ == "__main__":
    unittest.main()
