"""SQLite persistent idempotency store, sequential invoice allocator, and delivery audit ledger."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from client_onboarding_bot.models import ClientRecord

_INV_PATTERN = re.compile(r"^[A-Z]+-(\d{8})-(\d+)$")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS clients_onboarded (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    project_name TEXT NOT NULL,
    workspace_slug TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    workspace_mode TEXT NOT NULL DEFAULT 'local',
    drive_url TEXT NOT NULL DEFAULT '',
    invoice_number TEXT NOT NULL,
    currency_code TEXT NOT NULL DEFAULT 'USD',
    subtotal TEXT NOT NULL DEFAULT '0.00',
    discount_amount TEXT NOT NULL DEFAULT '0.00',
    tax_amount TEXT NOT NULL DEFAULT '0.00',
    total_amount TEXT NOT NULL DEFAULT '0.00',
    onboarded_date TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_sequences (
    sequence_date TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    input_source TEXT NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    onboarded_count INTEGER NOT NULL DEFAULT 0,
    partial_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS delivery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    external_ref TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(idempotency_key, channel)
);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Persistent SQLite state store guaranteeing idempotency and monotonic invoice numbering."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as conn:
            conn.executescript(_SCHEMA_SQL)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_client(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM clients_onboarded WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return dict(row) if row else None

    def is_slug_taken_by_other(self, workspace_slug: str, idempotency_key: str) -> bool:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM clients_onboarded WHERE workspace_slug = ? AND idempotency_key != ? LIMIT 1",
                (workspace_slug, idempotency_key),
            ).fetchone()
            return row is not None

    def seed_invoice_sequences(self, records: list[ClientRecord]) -> None:
        """Seed daily invoice sequence counters from existing CRM invoice numbers."""
        max_by_date: dict[str, int] = {}
        for rec in records:
            inv = rec.invoice_number.strip().upper()
            match = _INV_PATTERN.match(inv)
            if not match:
                continue
            ymd, seq_str = match.group(1), match.group(2)
            seq = int(seq_str)
            if seq > max_by_date.get(ymd, 0):
                max_by_date[ymd] = seq

        if not max_by_date:
            return

        with self._tx() as conn:
            for ymd, seq in max_by_date.items():
                conn.execute(
                    """
                    INSERT INTO invoice_sequences (sequence_date, last_seq)
                    VALUES (?, ?)
                    ON CONFLICT(sequence_date) DO UPDATE SET
                        last_seq = MAX(invoice_sequences.last_seq, excluded.last_seq)
                    """,
                    (ymd, seq),
                )

    def preview_next_invoice_number(self, prefix: str, issue_date: date) -> str:
        """Compute the next invoice number without mutating sequence state (for dry-run)."""
        ymd = issue_date.strftime("%Y%m%d")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT last_seq FROM invoice_sequences WHERE sequence_date = ?",
                (ymd,),
            ).fetchone()
            next_seq = (int(row["last_seq"]) if row else 0) + 1
        return f"{prefix}-{ymd}-{next_seq:03d}"

    def allocate_invoice_number(self, prefix: str, issue_date: date) -> str:
        """Atomically reserve the next strictly monotonic invoice number for issue_date."""
        ymd = issue_date.strftime("%Y%m%d")
        with self._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_seq FROM invoice_sequences WHERE sequence_date = ?",
                (ymd,),
            ).fetchone()
            next_seq = (int(row["last_seq"]) if row else 0) + 1
            conn.execute(
                """
                INSERT INTO invoice_sequences (sequence_date, last_seq)
                VALUES (?, ?)
                ON CONFLICT(sequence_date) DO UPDATE SET last_seq = excluded.last_seq
                """,
                (ymd, next_seq),
            )
        return f"{prefix}-{ymd}-{next_seq:03d}"

    def upsert_client(
        self,
        *,
        record: ClientRecord,
        workspace_slug: str,
        workspace_path: str,
        workspace_mode: str,
        drive_url: str,
        invoice_number: str,
        currency_code: str,
        subtotal: str,
        discount_amount: str,
        tax_amount: str,
        total_amount: str,
        onboarded_date: str,
        status: str,
    ) -> None:
        now = _utc_now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO clients_onboarded (
                    idempotency_key, client_id, client_name, contact_name, email, project_name,
                    workspace_slug, workspace_path, workspace_mode, drive_url,
                    invoice_number, currency_code, subtotal, discount_amount, tax_amount, total_amount,
                    onboarded_date, payload_hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    client_id = excluded.client_id,
                    client_name = excluded.client_name,
                    contact_name = excluded.contact_name,
                    email = excluded.email,
                    project_name = excluded.project_name,
                    workspace_slug = excluded.workspace_slug,
                    workspace_path = excluded.workspace_path,
                    workspace_mode = excluded.workspace_mode,
                    drive_url = excluded.drive_url,
                    invoice_number = excluded.invoice_number,
                    currency_code = excluded.currency_code,
                    subtotal = excluded.subtotal,
                    discount_amount = excluded.discount_amount,
                    tax_amount = excluded.tax_amount,
                    total_amount = excluded.total_amount,
                    onboarded_date = excluded.onboarded_date,
                    payload_hash = excluded.payload_hash,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    record.idempotency_key,
                    record.client_id,
                    record.client_name,
                    record.contact_name,
                    record.email,
                    record.project_name,
                    workspace_slug,
                    workspace_path,
                    workspace_mode,
                    drive_url,
                    invoice_number,
                    currency_code,
                    subtotal,
                    discount_amount,
                    tax_amount,
                    total_amount,
                    onboarded_date,
                    record.payload_hash,
                    status,
                    now,
                    now,
                ),
            )

    def record_delivery_event(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        channel: str,
        status: str,
        attempts: int = 1,
        last_error: str = "",
        external_ref: str = "",
    ) -> None:
        now = _utc_now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO delivery_events (
                    idempotency_key, run_id, channel, status, attempts, last_error, external_ref, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key, channel) DO UPDATE SET
                    run_id = excluded.run_id,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    last_error = excluded.last_error,
                    external_ref = excluded.external_ref,
                    updated_at = excluded.updated_at
                """,
                (idempotency_key, run_id, channel, status, attempts, last_error, external_ref, now),
            )

    def update_client_status(self, idempotency_key: str, status: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE clients_onboarded SET status = ?, updated_at = ? WHERE idempotency_key = ?",
                (status, _utc_now(), idempotency_key),
            )

    def get_failed_deliveries(self, channels: set[str] | None = None) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.idempotency_key, d.channel, d.status, d.attempts, d.last_error,
                       c.client_name, c.contact_name, c.email, c.project_name,
                       c.workspace_path, c.drive_url, c.invoice_number,
                       c.currency_code, c.total_amount, c.onboarded_date
                FROM delivery_events d
                JOIN clients_onboarded c ON c.idempotency_key = d.idempotency_key
                WHERE d.status = 'failed'
                ORDER BY d.updated_at ASC
                """
            ).fetchall()
            results = [dict(r) for r in rows]
            if channels:
                results = [r for r in results if r["channel"] in channels]
            return results

    def get_delivery_events_for_client(self, idempotency_key: str) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT channel, status, attempts, last_error, external_ref, updated_at "
                "FROM delivery_events WHERE idempotency_key = ? ORDER BY channel",
                (idempotency_key,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_clients(self, status_filter: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._tx() as conn:
            if status_filter:
                rows = conn.execute(
                    "SELECT * FROM clients_onboarded WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status_filter, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM clients_onboarded ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            items = [dict(r) for r in rows]
            for item in items:
                events = conn.execute(
                    "SELECT channel, status, attempts, last_error, external_ref "
                    "FROM delivery_events WHERE idempotency_key = ? ORDER BY channel",
                    (item["idempotency_key"],),
                ).fetchall()
                item["channels"] = [dict(e) for e in events]
            return items

    def record_run_start(
        self, *, run_id: str, started_at: str, mode: str, dry_run: bool, input_source: str, total_rows: int
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO onboarding_runs (
                    run_id, started_at, mode, dry_run, input_source, total_rows, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'running')
                """,
                (run_id, started_at, mode, int(dry_run), input_source, total_rows),
            )

    def record_run_finish(
        self,
        *,
        run_id: str,
        finished_at: str,
        onboarded_count: int,
        partial_count: int,
        skipped_count: int,
        failed_count: int,
        status: str,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE onboarding_runs
                SET finished_at = ?, onboarded_count = ?, partial_count = ?,
                    skipped_count = ?, failed_count = ?, status = ?
                WHERE run_id = ?
                """,
                (finished_at, onboarded_count, partial_count, skipped_count, failed_count, status, run_id),
            )

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM onboarding_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
