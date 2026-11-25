"""Unit tests for multi-format CRM ingestion, Decimal money parsing, and validation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from client_onboarding_bot.ingestion import (  # noqa: E402
    export_updated_records,
    load_crm_records,
    parse_discount,
    parse_money,
    parse_tax_rate,
    validate_email,
)


class TestIngestion(unittest.TestCase):
    def test_parse_money_variants(self) -> None:
        amt, code, sym = parse_money("$1,250.50")
        self.assertEqual(amt, Decimal("1250.50"))
        self.assertEqual(code, "USD")
        self.assertEqual(sym, "$")

        amt_sar, code_sar, sym_sar = parse_money("SAR 7,500")
        self.assertEqual(amt_sar, Decimal("7500.00"))
        self.assertEqual(code_sar, "SAR")
        self.assertEqual(sym_sar, "SAR ")

        amt_eur, code_eur, _ = parse_money("€900.255")
        self.assertEqual(amt_eur, Decimal("900.26"))
        self.assertEqual(code_eur, "EUR")

        self.assertEqual(parse_money("")[0], Decimal("0.00"))

        with self.assertRaises(ValueError):
            parse_money("TBD")
        with self.assertRaises(ValueError):
            parse_money("-100")

    def test_parse_discount_and_tax(self) -> None:
        self.assertEqual(parse_discount("10%"), (Decimal("0.10"), True))
        self.assertEqual(parse_discount("$50"), (Decimal("50.00"), False))
        self.assertEqual(parse_tax_rate("15%"), Decimal("0.15"))
        self.assertEqual(parse_tax_rate("0.08"), Decimal("0.08"))
        self.assertIsNone(parse_tax_rate(""))

        with self.assertRaises(ValueError):
            parse_discount("120%")
        with self.assertRaises(ValueError):
            parse_tax_rate("1.5")

    def test_validate_email(self) -> None:
        self.assertTrue(validate_email("alice@company.co.uk"))
        self.assertFalse(validate_email("not-an-email"))
        self.assertFalse(validate_email("missing@tld"))

    def test_load_csv_isolates_invalid_rows(self) -> None:
        csv_text = (
            "Client Name,Contact Name,Email,Project Name,Project Fee,Status,Extra Col\n"
            "Valid Co,Alice,alice@valid.com,Bot,$750,Won,VIP\n"
            "Bad Fee Co,Bob,bob@bad.com,Bot,TBD,Won,Standard\n"
            "Bad Email Co,Eve,bad-email,Bot,$500,Won,Standard\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clients.csv"
            path.write_text(csv_text, encoding="utf-8")
            loaded = load_crm_records(path)

            self.assertEqual(len(loaded.records), 1)
            self.assertEqual(loaded.records[0].client_name, "Valid Co")
            self.assertEqual(loaded.records[0].raw_row["Extra Col"], "VIP")
            self.assertEqual(len(loaded.errors), 2)

            out_csv = Path(tmp) / "out.csv"
            loaded.records[0].onboarded = True
            loaded.records[0].onboarded_date = "2026-11-25"
            loaded.records[0].invoice_number = "INV-20261125-001"
            export_updated_records(out_csv, loaded.records, loaded.fieldnames)
            exported = out_csv.read_text(encoding="utf-8")
            self.assertIn("Extra Col", exported)
            self.assertIn("INV-20261125-001", exported)

    def test_load_json_and_jsonl(self) -> None:
        payload = [
            {
                "Client ID": "C-1",
                "Client Name": "Nordic Data",
                "Email": "ops@nordic.example.com",
                "Project Name": "ETL Sync",
                "Project Fee": "1800",
                "Currency": "EUR",
                "Status": "Won",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            jpath = Path(tmp) / "clients.json"
            jpath.write_text(json.dumps(payload), encoding="utf-8")
            loaded_json = load_crm_records(jpath)
            self.assertEqual(len(loaded_json.records), 1)
            self.assertEqual(loaded_json.records[0].currency_code, "EUR")
            self.assertEqual(loaded_json.records[0].currency_symbol, "€")

            jlpath = Path(tmp) / "clients.jsonl"
            jlpath.write_text(json.dumps(payload[0]) + "\n", encoding="utf-8")
            loaded_jsonl = load_crm_records(jlpath)
            self.assertEqual(len(loaded_jsonl.records), 1)


if __name__ == "__main__":
    unittest.main()
