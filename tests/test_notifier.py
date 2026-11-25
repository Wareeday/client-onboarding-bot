"""Unit tests for email drafts, mocked SMTP retries, and Slack/Discord/JSON webhooks."""

from __future__ import annotations

import io
import json
import smtplib
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from client_onboarding_bot.config import RetrySettings, SMTPSettings, WebhookSettings, load_settings  # noqa: E402
from client_onboarding_bot.models import ClientRecord, InvoiceResult  # noqa: E402
from client_onboarding_bot.notifier import (  # noqa: E402
    build_webhook_payload,
    detect_webhook_format,
    send_email_smtp,
    send_webhook_alert,
    write_welcome_email_draft,
)


class TestNotifier(unittest.TestCase):
    def _sample_client_and_invoice(self, tmp: Path) -> tuple[ClientRecord, InvoiceResult]:
        client = ClientRecord(
            row_index=1,
            client_name="Bright Smile Dental",
            contact_name="Dr. Sarah Lin",
            email="sarah@brightsmile.example.com",
            project_name="AI Appointment Bot",
            project_fee=Decimal("750.00"),
            status="Won",
        )
        inv_html = tmp / "INV-20261125-001.html"
        inv_html.write_text("<html><body>Invoice</body></html>", encoding="utf-8")
        invoice = InvoiceResult(
            invoice_number="INV-20261125-001",
            issue_date=date(2026, 11, 25),
            due_date=date(2026, 12, 9),
            currency_code="USD",
            currency_symbol="$",
            subtotal=Decimal("750.00"),
            discount_amount=Decimal("0.00"),
            tax_label="Tax",
            tax_rate=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total=Decimal("750.00"),
            html_path=inv_html,
        )
        return client, invoice

    def test_write_welcome_draft_txt_and_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(output_dir=root / "out")
            client, invoice = self._sample_client_and_invoice(root)
            txt_p, html_p = write_welcome_email_draft(client, str(root), invoice, root, settings)
            self.assertTrue(txt_p.exists())
            self.assertTrue(html_p.exists())
            self.assertIn("Dr. Sarah Lin", txt_p.read_text(encoding="utf-8"))
            self.assertIn("INV-20261125-001", html_p.read_text(encoding="utf-8"))

    def test_smtp_retries_transient_and_aborts_auth_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = load_settings(output_dir=root / "out")
            settings.smtp = SMTPSettings(
                host="smtp.gmail.com",
                port=465,
                username="bot@example.com",
                password="secret",
                use_ssl=True,
                from_address="bot@example.com",
            )
            settings.retry = RetrySettings(max_attempts=3, base_delay_seconds=0.0, backoff_multiplier=1.0)
            client, invoice = self._sample_client_and_invoice(root)

            smtp_instance = MagicMock()
            smtp_instance.__enter__.return_value = smtp_instance
            smtp_instance.send_message.side_effect = [
                smtplib.SMTPServerDisconnected("transient drop"),
                None,
            ]

            with patch("smtplib.SMTP_SSL", return_value=smtp_instance):
                attempts = send_email_smtp(
                    to_email=client.email,
                    subject="Welcome",
                    plain_body="Hello",
                    html_body="<p>Hello</p>",
                    settings=settings,
                    attachments=[invoice.html_path] if invoice.html_path else None,
                    sleep_fn=lambda _: None,
                )
            self.assertEqual(attempts, 2)

            smtp_auth_fail = MagicMock()
            smtp_auth_fail.__enter__.return_value = smtp_auth_fail
            smtp_auth_fail.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad creds")
            with patch("smtplib.SMTP_SSL", return_value=smtp_auth_fail), self.assertRaises(
                smtplib.SMTPAuthenticationError
            ):
                send_email_smtp(
                    to_email=client.email,
                    subject="Welcome",
                    plain_body="Hello",
                    html_body="<p>Hello</p>",
                    settings=settings,
                    sleep_fn=lambda _: None,
                )
            self.assertEqual(smtp_auth_fail.login.call_count, 1)

    def test_webhook_dialects_and_retry(self) -> None:
        self.assertEqual(detect_webhook_format("https://hooks.slack.com/services/T/B/X"), "slack")
        self.assertEqual(detect_webhook_format("https://discord.com/api/webhooks/1/2"), "discord")
        self.assertEqual(detect_webhook_format("https://api.example.com/hook"), "generic")

        slack_payload = build_webhook_payload(
            client_name="Acme",
            project_name="Bot",
            invoice_number="INV-1",
            total_display="$750.00 USD",
            workspace_location="/tmp/acme",
            fmt="slack",
        )
        self.assertIn("blocks", slack_payload)

        discord_payload = build_webhook_payload(
            client_name="Acme",
            project_name="Bot",
            invoice_number="INV-1",
            total_display="$750.00 USD",
            workspace_location="/tmp/acme",
            fmt="discord",
        )
        self.assertIn("embeds", discord_payload)

        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(output_dir=Path(tmp) / "out")
            settings.webhook = WebhookSettings(url="https://hooks.slack.com/services/T/B/X", fmt="auto")
            settings.retry = RetrySettings(max_attempts=3, base_delay_seconds=0.0, backoff_multiplier=1.0)

            ok_resp = MagicMock()
            ok_resp.__enter__.return_value = ok_resp
            ok_resp.status = 200
            err_429 = urllib.error.HTTPError(
                settings.webhook.url,
                429,
                "Too Many Requests",
                hdrs=None,  # type: ignore[arg-type]
                fp=io.BytesIO(b"rate limited"),
            )
            with patch("urllib.request.urlopen", side_effect=[err_429, ok_resp]) as mocked:
                attempts, ref = send_webhook_alert(
                    client_name="Acme",
                    project_name="Bot",
                    invoice_number="INV-1",
                    total_display="$750.00 USD",
                    workspace_location="/tmp/acme",
                    settings=settings,
                    sleep_fn=lambda _: None,
                )
            self.assertEqual(attempts, 2)
            self.assertEqual(ref, "slack:HTTP_200")
            sent_body = json.loads(mocked.call_args[0][0].data.decode("utf-8"))
            self.assertIn("blocks", sent_body)


if __name__ == "__main__":
    unittest.main()
