"""Multi-format CRM ingestion, financial/email validation, and export helpers."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from client_onboarding_bot.models import ClientRecord, RowValidationError

CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "SAR": "SAR ",
    "AED": "AED ",
    "CAD": "CA$",
    "AUD": "A$",
    "INR": "₹",
    "JPY": "¥",
    "CHF": "CHF ",
}

_SYMBOL_PREFIXES: list[tuple[str, str, str]] = [
    ("SAR", "SAR", "SAR "),
    ("AED", "AED", "AED "),
    ("CHF", "CHF", "CHF "),
    ("USD", "USD", "$"),
    ("EUR", "EUR", "€"),
    ("GBP", "GBP", "£"),
    ("CAD", "CAD", "CA$"),
    ("AUD", "AUD", "A$"),
    ("INR", "INR", "₹"),
    ("JPY", "JPY", "¥"),
    ("CA$", "CAD", "CA$"),
    ("A$", "AUD", "A$"),
    ("$", "USD", "$"),
    ("€", "EUR", "€"),
    ("£", "GBP", "£"),
    ("₹", "INR", "₹"),
    ("¥", "JPY", "¥"),
]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REQUIRED_FIELDS = ("Client Name", "Status")


@dataclass
class LoadedCRM:
    """Result of loading and validating a CRM CSV/JSON/JSONL file."""

    records: list[ClientRecord]
    errors: list[RowValidationError]
    fieldnames: list[str]
    source_format: str


def parse_money(
    raw: Any,
    *,
    allow_negative: bool = False,
) -> tuple[Decimal, str | None, str | None]:
    """Parse monetary inputs like '$1,250.50', 'SAR 5,000', or '750' into Decimal.

    Returns (amount, inferred_currency_code, inferred_currency_symbol).
    """
    if raw is None:
        return Decimal("0.00"), None, None

    text = str(raw).strip()
    if not text:
        return Decimal("0.00"), None, None

    inferred_code: str | None = None
    inferred_symbol: str | None = None
    cleaned = text

    for token, code, symbol in _SYMBOL_PREFIXES:
        if token.isalpha():
            pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
            if pattern.search(cleaned):
                inferred_code = code
                inferred_symbol = symbol
                cleaned = pattern.sub("", cleaned)
        elif token in cleaned:
            inferred_code = code
            inferred_symbol = symbol
            cleaned = cleaned.replace(token, "")

    cleaned = cleaned.replace(",", "").strip()
    if not re.match(r"^-?\d+(?:\.\d+)?$", cleaned):
        raise ValueError(f"Invalid monetary amount: '{text}'")

    amount = Decimal(cleaned).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not allow_negative and amount < Decimal("0.00"):
        raise ValueError(f"Monetary amount cannot be negative: '{text}'")
    return amount, inferred_code, inferred_symbol


def parse_discount(raw: Any) -> tuple[Decimal, bool]:
    """Parse flat ('$50') or percentage ('10%') discount value."""
    text = str(raw or "").strip()
    if not text:
        return Decimal("0.00"), False
    if text.endswith("%"):
        pct_str = text[:-1].strip()
        try:
            pct = Decimal(pct_str)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid discount percentage: '{text}'") from exc
        if pct < Decimal("0") or pct > Decimal("100"):
            raise ValueError(f"Discount percentage must be 0-100%: '{text}'")
        return (pct / Decimal("100")), True
    amount, _, _ = parse_money(text, allow_negative=False)
    return amount, False


def parse_tax_rate(raw: Any) -> Decimal | None:
    """Parse optional tax rate ('15%' or '0.15') into a decimal fraction."""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("%"):
        try:
            rate = Decimal(text[:-1].strip()) / Decimal("100")
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid tax rate: '{text}'") from exc
    else:
        try:
            rate = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid tax rate: '{text}'") from exc
    if rate < Decimal("0") or rate > Decimal("1"):
        raise ValueError(f"Tax rate must be between 0 and 100% (got '{text}')")
    return rate


def parse_optional_date(raw: Any) -> date | None:
    """Parse ISO YYYY-MM-DD date string when provided."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date '{text}'; expected YYYY-MM-DD.") from exc


def validate_email(email: str) -> bool:
    """Check whether a non-empty email address matches basic RFC structure."""
    return bool(_EMAIL_RE.match(email.strip()))


def _load_raw_rows(filepath: Path) -> tuple[list[dict[str, str]], list[str], str]:
    if not filepath.exists():
        raise FileNotFoundError(f"CRM input file not found: {filepath}")

    suffix = filepath.suffix.lower()
    if suffix == ".csv":
        with filepath.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError(f"CSV file '{filepath}' has no header row.")
            fieldnames = [f.strip() for f in reader.fieldnames if f]
            rows: list[dict[str, str]] = []
            for raw in reader:
                normalized = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
                if any(normalized.values()):
                    rows.append(normalized)
            return rows, fieldnames, "csv"

    if suffix == ".json":
        payload = json.loads(filepath.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("clients"), list):
            items = payload["clients"]
        elif isinstance(payload, list):
            items = payload
        else:
            raise ValueError("JSON input must be a list of client objects or {'clients': [...]}.")
        return _normalize_json_rows(items, "json")

    if suffix == ".jsonl":
        items = [
            json.loads(line)
            for line in filepath.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return _normalize_json_rows(items, "jsonl")

    raise ValueError(f"Unsupported CRM input format '{suffix}'. Use .csv, .json, or .jsonl.")


def _normalize_json_rows(items: list[Any], fmt: str) -> tuple[list[dict[str, str]], list[str], str]:
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    seen_fields: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each record in JSON/JSONL input must be a JSON object.")
        row: dict[str, str] = {}
        for k, v in item.items():
            key = str(k).strip()
            row[key] = "" if v is None else str(v).strip()
            if key not in seen_fields:
                seen_fields.add(key)
                fieldnames.append(key)
        if any(row.values()):
            rows.append(row)
    return rows, fieldnames, fmt


def load_crm_records(filepath: Path) -> LoadedCRM:
    """Load, normalize, and validate CRM client records from CSV, JSON, or JSONL."""
    raw_rows, fieldnames, source_format = _load_raw_rows(filepath)
    missing_headers = [col for col in _REQUIRED_FIELDS if col not in fieldnames]
    if missing_headers and raw_rows:
        raise ValueError(f"Missing required column(s) in {filepath}: {', '.join(missing_headers)}")

    records: list[ClientRecord] = []
    errors: list[RowValidationError] = []

    for idx, row in enumerate(raw_rows, start=1):
        client_name = row.get("Client Name", "").strip()
        status = row.get("Status", "").strip()
        row_errors: list[RowValidationError] = []

        if not client_name:
            row_errors.append(RowValidationError(idx, "(missing)", "Client Name", "Client Name is required."))
        if not status:
            row_errors.append(RowValidationError(idx, client_name or "(missing)", "Status", "Status is required."))

        email = row.get("Email", "").strip()
        if email and not validate_email(email):
            row_errors.append(RowValidationError(idx, client_name, "Email", f"Invalid email syntax: '{email}'."))

        fee = Decimal("0.00")
        inferred_code: str | None = None
        inferred_symbol: str | None = None
        try:
            fee, inferred_code, inferred_symbol = parse_money(row.get("Project Fee", "0"))
        except ValueError as exc:
            row_errors.append(RowValidationError(idx, client_name, "Project Fee", str(exc)))

        discount = Decimal("0.00")
        discount_is_percent = False
        try:
            discount, discount_is_percent = parse_discount(row.get("Discount", ""))
        except ValueError as exc:
            row_errors.append(RowValidationError(idx, client_name, "Discount", str(exc)))

        tax_rate: Decimal | None = None
        try:
            tax_rate = parse_tax_rate(row.get("Tax Rate", ""))
        except ValueError as exc:
            row_errors.append(RowValidationError(idx, client_name, "Tax Rate", str(exc)))

        start_dt: date | None = None
        try:
            start_dt = parse_optional_date(row.get("Start Date", ""))
        except ValueError as exc:
            row_errors.append(RowValidationError(idx, client_name, "Start Date", str(exc)))

        payment_terms: int | None = None
        terms_raw = row.get("Payment Terms Days", "").strip()
        if terms_raw:
            try:
                payment_terms = int(terms_raw)
                if payment_terms < 0:
                    raise ValueError
            except ValueError:
                row_errors.append(
                    RowValidationError(idx, client_name, "Payment Terms Days", f"Must be a non-negative integer: '{terms_raw}'.")
                )

        if row_errors:
            errors.extend(row_errors)
            continue

        explicit_currency = row.get("Currency", "").strip().upper()
        currency_code = explicit_currency or inferred_code
        currency_symbol = (
            CURRENCY_SYMBOLS.get(explicit_currency, f"{explicit_currency} ")
            if explicit_currency
            else inferred_symbol
        )

        onboarded_flag = row.get("Onboarded", "").strip().lower() in {"yes", "true", "1"}

        records.append(
            ClientRecord(
                row_index=idx,
                client_id=row.get("Client ID", "").strip(),
                client_name=client_name,
                contact_name=row.get("Contact Name", "").strip(),
                email=email,
                project_name=row.get("Project Name", "").strip() or "Automation Setup",
                project_fee=fee,
                status=status,
                onboarded=onboarded_flag,
                onboarded_date=row.get("Onboarded Date", "").strip(),
                invoice_number=row.get("Invoice Number", "").strip(),
                discount=discount,
                discount_is_percent=discount_is_percent,
                tax_rate=tax_rate,
                currency_code=currency_code,
                currency_symbol=currency_symbol,
                payment_terms_days=payment_terms,
                start_date=start_dt,
                tier=row.get("Tier", "").strip(),
                notes=row.get("Notes", "").strip(),
                raw_row=dict(row),
            )
        )

    return LoadedCRM(records=records, errors=errors, fieldnames=fieldnames, source_format=source_format)


def export_updated_records(
    filepath: Path,
    records: list[ClientRecord],
    original_fieldnames: list[str] | None = None,
) -> Path:
    """Write updated client tracking state back to CSV or JSON while preserving CRM columns."""
    filepath.parent.mkdir(parents=True, exist_ok=True)

    output_rows: list[dict[str, str]] = []
    fieldnames = list(original_fieldnames or [])
    for required_out in ("Onboarded", "Onboarded Date", "Invoice Number"):
        if required_out not in fieldnames:
            fieldnames.append(required_out)

    for rec in records:
        row = dict(rec.raw_row)
        row["Onboarded"] = "Yes" if rec.onboarded else (row.get("Onboarded") or "No")
        row["Onboarded Date"] = rec.onboarded_date or row.get("Onboarded Date", "")
        row["Invoice Number"] = rec.invoice_number or row.get("Invoice Number", "")
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
        output_rows.append(row)

    if filepath.suffix.lower() == ".json":
        filepath.write_text(json.dumps(output_rows, indent=2), encoding="utf-8")
        return filepath

    if not output_rows and not fieldnames:
        fieldnames = ["Client Name", "Contact Name", "Email", "Project Name", "Project Fee", "Status", "Onboarded", "Onboarded Date", "Invoice Number"]

    with filepath.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)
    return filepath
