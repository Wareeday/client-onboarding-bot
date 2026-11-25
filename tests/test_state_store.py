"""Unit tests for SQLite StateStore idempotency, invoice sequences, and delivery ledger."""

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

from client_onboarding_bot.models import ClientRecord  # noqa: E402
from client_onboarding_bot.state_store import StateStore  # noqa: E402


class TestStateStore(unittest.TestCase):
    def test_invoice_sequence_seeding_and_monotonic_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "onboarding.db")
            existing = [
                ClientRecord(
                    row_index=1,
                    client_name="Historic Client",
                    contact_name="Alex",
                    email="alex@hist.example.com",
                    project_name="Audit",
                    project_fee=Decimal("400.00"),
                    status="Won",
                    onboarded=True,
                    invoice_number="INV-20261125-003",
                )
            ]
            store.seed_invoice_sequences(existing)

            self.assertEqual(
                store.preview_next_invoice_number("INV", date(2026, 11, 25)),
                "INV-20261125-004",
            )
            inv1 = store.allocate_invoice_number("INV", date(2026, 11, 25))
            inv2 = store.allocate_invoice_number("INV", date(2026, 11, 25))
            self.assertEqual(inv1, "INV-20261125-004")
            self.assertEqual(inv2, "INV-20261125-005")

    def test_failed_deliveries_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "onboarding.db")
            rec = ClientRecord(
                row_index=1,
                client_name="Delta Co",
                contact_name="Sam",
                email="sam@delta.example.com",
                project_name="CRM Sync",
                project_fee=Decimal("900.00"),
                status="Won",
            )
            store.upsert_client(
                record=rec,
                workspace_slug="Delta_Co",
                workspace_path=str(Path(tmp) / "Delta_Co"),
                workspace_mode="local",
                drive_url="",
                invoice_number="INV-20261125-001",
                currency_code="USD",
                subtotal="900.00",
                discount_amount="0.00",
                tax_amount="0.00",
                total_amount="900.00",
                onboarded_date="2026-11-25",
                status="partial",
            )
            store.record_delivery_event(
                idempotency_key=rec.idempotency_key,
                run_id="run-1",
                channel="email_smtp",
                status="failed",
                attempts=3,
                last_error="SMTP timeout",
            )
            failed = store.get_failed_deliveries()
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["client_name"], "Delta Co")
            self.assertEqual(failed[0]["channel"], "email_smtp")


if __name__ == "__main__":
    unittest.main()
