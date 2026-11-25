"""Domain models for client onboarding records, invoices, channel statuses, and run summaries."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any


def _normalize_key_part(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


@dataclass(frozen=True)
class MilestoneSpec:
    """Definition of a project kickoff milestone relative to onboarding start date."""

    name: str
    day_offset: int
    status: str = "Not Started"
    notes: str = ""


@dataclass
class ClientRecord:
    """Normalized CRM client record validated for onboarding."""

    row_index: int
    client_name: str
    contact_name: str
    email: str
    project_name: str
    project_fee: Decimal
    status: str
    onboarded: bool = False
    onboarded_date: str = ""
    invoice_number: str = ""
    client_id: str = ""
    discount: Decimal = Decimal("0.00")
    discount_is_percent: bool = False
    tax_rate: Decimal | None = None
    currency_code: str | None = None
    currency_symbol: str | None = None
    payment_terms_days: int | None = None
    start_date: date | None = None
    tier: str = ""
    notes: str = ""
    raw_row: dict[str, str] = field(default_factory=dict)

    @property
    def is_won(self) -> bool:
        return self.status.strip().lower() == "won"

    @property
    def idempotency_key(self) -> str:
        """Stable deterministic key preventing duplicate onboarding across runs."""
        if self.client_id.strip():
            return f"cid:{_normalize_key_part(self.client_id)}"
        parts = [
            _normalize_key_part(self.client_name),
            _normalize_key_part(self.project_name),
            _normalize_key_part(self.email),
        ]
        return "key:" + "|".join(parts)

    @property
    def payload_hash(self) -> str:
        """Canonical SHA-256 fingerprint of commercial onboarding attributes."""
        canonical = {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "email": self.email.lower(),
            "project_name": self.project_name,
            "project_fee": str(self.project_fee),
            "discount": str(self.discount),
            "discount_is_percent": self.discount_is_percent,
            "tax_rate": str(self.tax_rate) if self.tax_rate is not None else None,
            "currency_code": self.currency_code,
        }
        encoded = json.dumps(canonical, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RowValidationError:
    """Validation issue discovered in a CRM input row."""

    row_index: int
    client_name: str
    field: str
    message: str


@dataclass(frozen=True)
class InvoiceResult:
    """Generated invoice artifacts and exact Decimal financial totals."""

    invoice_number: str
    issue_date: date
    due_date: date
    currency_code: str
    currency_symbol: str
    subtotal: Decimal
    discount_amount: Decimal
    tax_label: str
    tax_rate: Decimal
    tax_amount: Decimal
    total: Decimal
    md_path: Path | None = None
    html_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_number": self.invoice_number,
            "issue_date": self.issue_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "currency_code": self.currency_code,
            "currency_symbol": self.currency_symbol,
            "subtotal": str(self.subtotal),
            "discount_amount": str(self.discount_amount),
            "tax_label": self.tax_label,
            "tax_rate": str(self.tax_rate),
            "tax_amount": str(self.tax_amount),
            "total": str(self.total),
            "md_path": str(self.md_path) if self.md_path else None,
            "html_path": str(self.html_path) if self.html_path else None,
        }


@dataclass(frozen=True)
class ChannelStatus:
    """Outcome of executing an onboarding side-effect channel."""

    channel: str
    status: str  # "success" | "skipped" | "failed" | "dry_run"
    attempts: int = 1
    detail: str = ""
    external_ref: str = ""


@dataclass
class ClientOnboardingOutcome:
    """Execution outcome for a single client record."""

    client_name: str
    idempotency_key: str
    status: str  # "onboarded" | "partial" | "skipped" | "failed" | "dry_run"
    reason: str = ""
    workspace_location: str = ""
    workspace_mode: str = "local"
    invoice: InvoiceResult | None = None
    channels: list[ChannelStatus] = field(default_factory=list)
    error: str = ""


@dataclass
class RunSummary:
    """Aggregate report for a pipeline execution."""

    run_id: str
    started_at: str
    finished_at: str
    mode: str
    dry_run: bool
    input_source: str
    total_rows: int
    validation_errors: list[RowValidationError] = field(default_factory=list)
    onboarded_count: int = 0
    partial_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    dry_run_count: int = 0
    total_revenue_by_currency: dict[str, str] = field(default_factory=dict)
    outcomes: list[ClientOnboardingOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "input_source": self.input_source,
            "total_rows": self.total_rows,
            "onboarded_count": self.onboarded_count,
            "partial_count": self.partial_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "dry_run_count": self.dry_run_count,
            "total_revenue_by_currency": self.total_revenue_by_currency,
            "validation_errors": [
                {
                    "row_index": e.row_index,
                    "client_name": e.client_name,
                    "field": e.field,
                    "message": e.message,
                }
                for e in self.validation_errors
            ],
            "outcomes": [
                {
                    "client_name": o.client_name,
                    "idempotency_key": o.idempotency_key,
                    "status": o.status,
                    "reason": o.reason,
                    "workspace_location": o.workspace_location,
                    "workspace_mode": o.workspace_mode,
                    "invoice": o.invoice.to_dict() if o.invoice else None,
                    "channels": [
                        {
                            "channel": c.channel,
                            "status": c.status,
                            "attempts": c.attempts,
                            "detail": c.detail,
                            "external_ref": c.external_ref,
                        }
                        for c in o.channels
                    ],
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }
