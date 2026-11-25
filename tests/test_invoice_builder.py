"""Unit tests for Decimal invoicing engine and XSS-safe HTML5 rendering."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from client_onboarding_bot.config import load_settings  # noqa: E402
from client_onboarding_bot.invoice_builder import compute_invoice_amounts, generate_invoice  # noqa: E402
from client_onboarding_bot.models import ClientRecord  # noqa: E402


class TestInvoiceBuilder(unittest.TestCase):
    def test_decimal_discount_and_tax_calculation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(output_dir=Path(tmp) / "out")
            client = ClientRecord(
                row_index=1,
                client_name="Precision Labs",
                contact_name="Dana",
                email="dana@precision.example.com",
                project_name="Automation Suite",
                project_fee=Decimal("1000.00"),
                discount=Decimal("0.10"),
                discount_is_percent=True,
                tax_rate=Decimal("0.15"),
                currency_code="SAR",
                currency_symbol="SAR ",
                payment_terms_days=30,
                status="Won",
            )
            subtotal, discount_amt, tax_rate, tax_amt, total, code, sym = compute_invoice_amounts(
                client, settings
            )
            self.assertEqual(subtotal, Decimal("1000.00"))
            self.assertEqual(discount_amt, Decimal("100.00"))
            self.assertEqual(tax_rate, Decimal("0.15"))
            self.assertEqual(tax_amt, Decimal("135.00"))
            self.assertEqual(total, Decimal("1035.00"))
            self.assertEqual(code, "SAR")
            self.assertEqual(sym, "SAR ")

            inv = generate_invoice(
                client=client,
                invoices_dir=Path(tmp) / "invoices",
                invoice_number="INV-20261125-001",
                settings=settings,
                issue_date=date(2026, 11, 25),
            )
            self.assertEqual(inv.due_date, date(2026, 12, 25))
            self.assertTrue(inv.md_path and inv.md_path.exists())
            self.assertTrue(inv.html_path and inv.html_path.exists())
            html_text = inv.html_path.read_text(encoding="utf-8")
            self.assertIn("SAR 1,035.00", html_text)
            self.assertIn("@media print", html_text)

    def test_html_xss_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(output_dir=Path(tmp) / "out")
            client = ClientRecord(
                row_index=1,
                client_name="A & B <script>alert(1)</script> Corp",
                contact_name="<img src=x onerror=alert(2)>",
                email="sec@example.com",
                project_name="<b>Bot</b>",
                project_fee=Decimal("500.00"),
                status="Won",
            )
            inv = generate_invoice(
                client=client,
                invoices_dir=Path(tmp) / "invoices",
                invoice_number="INV-20261125-002",
                settings=settings,
                issue_date=date(2026, 11, 25),
            )
            assert inv.html_path is not None
            html_text = inv.html_path.read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)</script>", html_text)
            self.assertNotIn("<img src=x", html_text)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_text)


if __name__ == "__main__":
    unittest.main()
