"""End-to-end tests for OnboardingPipeline, idempotency, dry-run, retry-failed, config, and CLI."""

from __future__ import annotations

import contextlib
import io
import json
import smtplib
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from client_onboarding_bot.cli import main as cli_main  # noqa: E402
from client_onboarding_bot.config import load_settings  # noqa: E402
from client_onboarding_bot.logging_utils import configure_logging  # noqa: E402
from client_onboarding_bot.pipeline import OnboardingPipeline  # noqa: E402
from client_onboarding_bot.state_store import StateStore  # noqa: E402


class TestPipelineE2E(unittest.TestCase):
    def test_dry_run_creates_zero_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "clients.csv"
            csv_path.write_text(
                "Client Name,Contact Name,Email,Project Name,Project Fee,Status,Onboarded\n"
                "Bright Smile,Dr. Lin,lin@smile.example.com,Bot,$750,Won,No\n",
                encoding="utf-8",
            )
            out_dir = root / "output"
            exit_code = cli_main(
                ["--dry-run", "--input-csv", str(csv_path), "--output-dir", str(out_dir)]
            )
            self.assertEqual(exit_code, 0)
            self.assertFalse(out_dir.exists())

    def test_idempotency_across_repeated_runs_same_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "clients.csv"
            csv_path.write_text(
                "Client Name,Contact Name,Email,Project Name,Project Fee,Status,Onboarded,Onboarded Date,Invoice Number\n"
                "Bright Smile,Dr. Lin,lin@smile.example.com,AI Bot,$750,Won,No,,\n"
                "Apex Prospect,Dr. Khan,khan@apex.example.com,Sync,$500,Prospect,No,,\n"
                "Old Won,Marcus,m@old.example.com,Alerts,$600,Won,Yes,2026-11-10,INV-20261110-001\n",
                encoding="utf-8",
            )
            out_dir = root / "output"
            settings = load_settings(output_dir=out_dir)
            logger = configure_logging(log_dir=None)
            pipeline = OnboardingPipeline(settings, logger)

            run1 = pipeline.run(csv_path, issue_date=date(2026, 11, 25))
            self.assertEqual(run1.onboarded_count, 1)
            self.assertEqual(run1.skipped_count, 2)
            self.assertTrue((out_dir / "clients_updated.csv").exists())
            self.assertTrue((out_dir / "reports" / "latest_run.json").exists())
            self.assertTrue((out_dir / "reports" / "onboarding_dashboard.html").exists())

            # Re-run on the exact same unmodified CSV: SQLite idempotency prevents duplicate onboarding!
            run2 = pipeline.run(csv_path, issue_date=date(2026, 11, 25))
            self.assertEqual(run2.onboarded_count, 0)
            self.assertEqual(run2.skipped_count, 3)

    def test_error_isolation_and_retry_failed_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "clients.csv"
            csv_path.write_text(
                "Client Name,Contact Name,Email,Project Name,Project Fee,Status,Onboarded\n"
                "Client Alpha,A,alpha@example.com,Bot 1,$500,Won,No\n"
                "Client Beta,B,beta@example.com,Bot 2,$800,Won,No\n",
                encoding="utf-8",
            )
            env_path = root / ".env"
            env_path.write_text(
                "GMAIL_ADDRESS=bot@example.com\nGMAIL_APP_PASSWORD=app-pass-16\n",
                encoding="utf-8",
            )
            out_dir = root / "output"
            settings = load_settings(output_dir=out_dir, env_file=env_path)
            settings.retry.base_delay_seconds = 0.0
            logger = configure_logging(log_dir=None)
            pipeline = OnboardingPipeline(settings, logger)

            # Fail SMTP for Client Alpha (auth error -> non-retryable), succeed for Client Beta.
            smtp_fail = MagicMock()
            smtp_fail.__enter__.return_value = smtp_fail
            smtp_fail.login.side_effect = [
                smtplib.SMTPAuthenticationError(535, b"Temporary auth block"),
                None,
            ]

            with patch("smtplib.SMTP_SSL", return_value=smtp_fail):
                summary = pipeline.run(csv_path, send_emails=True, issue_date=date(2026, 11, 25))

            self.assertEqual(summary.partial_count, 1)
            self.assertEqual(summary.onboarded_count, 1)

            store = StateStore(settings.db_path)
            failed_before = store.get_failed_deliveries()
            self.assertEqual(len(failed_before), 1)
            self.assertEqual(failed_before[0]["client_name"], "Client Alpha")

            # Retry failed deliveries: only Client Alpha email is retried and promoted to completed.
            smtp_ok = MagicMock()
            smtp_ok.__enter__.return_value = smtp_ok
            with patch("smtplib.SMTP_SSL", return_value=smtp_ok):
                retry_res = pipeline.retry_failed(channel_filter="email_smtp")

            self.assertEqual(retry_res, {"retried": 1, "succeeded": 1, "failed": 0})
            self.assertEqual(store.get_failed_deliveries(), [])
            clients_in_db = {c["client_name"]: c for c in store.list_clients()}
            self.assertEqual(clients_in_db["Client Alpha"]["status"], "completed")
            self.assertEqual(clients_in_db["Client Beta"]["status"], "completed")

    def test_cli_subcommands_validate_onboard_status_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "out"
            cfg_json = root / "custom.json"
            cfg_json.write_text(
                json.dumps(
                    {
                        "agency": {
                            "agency_name": "Kinetix Automation",
                            "sender_name": "Lead Eng",
                            "sender_email": "ops@kinetix.example.com",
                            "tax_rate": 0.10,
                            "tax_label": "VAT",
                        }
                    }
                ),
                encoding="utf-8",
            )
            sample_json = Path(__file__).resolve().parents[1] / "sample_data" / "clients_enterprise.json"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(
                    cli_main(["validate", "--input", str(sample_json), "--config", str(cfg_json)]),
                    0,
                )
                self.assertEqual(
                    cli_main(
                        [
                            "onboard",
                            "--input",
                            str(sample_json),
                            "--config",
                            str(cfg_json),
                            "--output-dir",
                            str(out_dir),
                        ]
                    ),
                    0,
                )

            status_out = io.StringIO()
            with contextlib.redirect_stdout(status_out):
                self.assertEqual(
                    cli_main(["status", "--output-dir", str(out_dir), "--json"]),
                    0,
                )
            status_payload = json.loads(status_out.getvalue())
            self.assertEqual(len(status_payload["clients"]), 2)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(["report", "--output-dir", str(out_dir), "--config", str(cfg_json)]),
                    0,
                )
            dashboard = (out_dir / "reports" / "onboarding_dashboard.html").read_text(encoding="utf-8")
            self.assertIn("Kinetix Automation", dashboard)
            self.assertIn("Meridian Cloud Systems", dashboard)
            self.assertIn("Crescent Retail Group", dashboard)

    def test_client_filter_force_and_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "clients.csv"
            csv_path.write_text(
                "Client Name,Contact Name,Email,Project Name,Project Fee,Status,Onboarded,Onboarded Date,Invoice Number\n"
                "Alpha Co,A,a@alpha.example.com,Bot A,$400,Won,No,,\n"
                "Beta Co,B,b@beta.example.com,Bot B,$600,Won,No,,\n",
                encoding="utf-8",
            )
            out_dir = root / "out"
            settings = load_settings(output_dir=out_dir)
            logger = configure_logging(log_dir=None)
            pipeline = OnboardingPipeline(settings, logger)

            s1 = pipeline.run(csv_path, client_filter="Alpha Co", in_place=True, issue_date=date(2026, 11, 25))
            self.assertEqual(s1.onboarded_count, 1)
            in_place_text = csv_path.read_text(encoding="utf-8")
            self.assertIn("INV-20261125-001", in_place_text)
            self.assertIn("Beta Co,B,b@beta.example.com,Bot B,$600,Won,No,,", in_place_text)

            # Force re-onboard Alpha Co: keeps same slug, allocates next sequential invoice number.
            s2 = pipeline.run(csv_path, client_filter="Alpha Co", force=True, issue_date=date(2026, 11, 25))
            self.assertEqual(s2.onboarded_count, 1)
            assert s2.outcomes[0].invoice is not None
            self.assertEqual(s2.outcomes[0].invoice.invoice_number, "INV-20261125-002")


if __name__ == "__main__":
    unittest.main()
