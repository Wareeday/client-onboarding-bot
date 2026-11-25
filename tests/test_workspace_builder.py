"""Unit tests for local workspace provisioning, slug collision handling, and trackers."""

from __future__ import annotations

import csv
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
from client_onboarding_bot.models import ClientRecord  # noqa: E402
from client_onboarding_bot.state_store import StateStore  # noqa: E402
from client_onboarding_bot.workspace_builder import (  # noqa: E402
    create_local_workspace,
    resolve_workspace_slug,
    slugify,
)


class TestWorkspaceBuilder(unittest.TestCase):
    def test_slugify_and_collision_resolution(self) -> None:
        self.assertEqual(slugify("Bright Smile Dental!"), "Bright_Smile_Dental")
        self.assertEqual(slugify("@@@"), "Client")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            settings = load_settings(output_dir=out)
            store = StateStore(settings.db_path)

            c1 = ClientRecord(
                row_index=1,
                client_name="Acme Corp",
                contact_name="Alice",
                email="alice@acme1.example.com",
                project_name="Bot A",
                project_fee=Decimal("500.00"),
                status="Won",
            )
            slug1 = resolve_workspace_slug(c1, settings.clients_dir, store=store)
            self.assertEqual(slug1, "Acme_Corp")
            paths1 = create_local_workspace(c1, settings, slug=slug1, start_date=date(2026, 11, 25))
            store.upsert_client(
                record=c1,
                workspace_slug=slug1,
                workspace_path=str(paths1["root"]),
                workspace_mode="local",
                drive_url="",
                invoice_number="INV-20261125-001",
                currency_code="USD",
                subtotal="500.00",
                discount_amount="0.00",
                tax_amount="0.00",
                total_amount="500.00",
                onboarded_date="2026-11-25",
                status="completed",
            )

            # Distinct client whose name normalizes to the exact same base slug must disambiguate.
            c2 = ClientRecord(
                row_index=2,
                client_name="Acme Corp!",
                contact_name="Bob",
                email="bob@acme2.example.com",
                project_name="Bot B",
                project_fee=Decimal("600.00"),
                status="Won",
            )
            slug2 = resolve_workspace_slug(c2, settings.clients_dir, store=store)
            self.assertEqual(slug2, "Acme_Corp_2")

    def test_tracker_and_kickoff_brief_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_settings(output_dir=Path(tmp) / "out")
            client = ClientRecord(
                row_index=1,
                client_name="Bright Smile Dental",
                contact_name="Dr. Sarah Lin",
                email="hello@brightsmile.example.com",
                project_name="AI Appointment Bot",
                project_fee=Decimal("750.00"),
                status="Won",
                tier="Growth",
            )
            paths = create_local_workspace(client, settings, start_date=date(2026, 11, 20))
            self.assertTrue(paths["tracker"].exists())
            self.assertTrue(paths["brief"].exists())
            for sub in settings.subfolders:
                self.assertTrue(paths[sub].exists())

            with paths["tracker"].open("r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), len(settings.milestones))
            self.assertEqual(rows[0]["Target Date"], "2026-11-21")

            brief_text = paths["brief"].read_text(encoding="utf-8")
            self.assertIn("Bright Smile Dental", brief_text)
            self.assertIn("Growth", brief_text)


if __name__ == "__main__":
    unittest.main()
