"""Core onboarding pipeline orchestrator with idempotency, error isolation, dry-run, and retries."""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from client_onboarding_bot.config import PipelineSettings
from client_onboarding_bot.ingestion import export_updated_records, load_crm_records
from client_onboarding_bot.invoice_builder import compute_invoice_amounts, generate_invoice
from client_onboarding_bot.models import (
    ChannelStatus,
    ClientOnboardingOutcome,
    ClientRecord,
    RunSummary,
)
from client_onboarding_bot.notifier import (
    render_welcome_email,
    send_email_smtp,
    send_webhook_alert,
    write_welcome_email_draft,
)
from client_onboarding_bot.reporting import write_run_report
from client_onboarding_bot.state_store import StateStore
from client_onboarding_bot.workspace_builder import (
    create_google_drive_workspace,
    create_local_workspace,
    resolve_workspace_slug,
)

_INV_PATTERN = re.compile(r"^[A-Z]+-(\d{8})-(\d+)$")


class OnboardingPipeline:
    """Orchestrates end-to-end client onboarding across workspace, invoicing, and notifications."""

    def __init__(self, settings: PipelineSettings, logger: logging.Logger) -> None:
        self.settings = settings
        self.logger = logger

    def run(
        self,
        input_path: Path,
        *,
        mode: str = "local",
        dry_run: bool = False,
        send_emails: bool = False,
        attach_invoice: bool = False,
        notify_webhook: bool = False,
        client_filter: str | None = None,
        force: bool = False,
        in_place: bool = False,
        issue_date: date | None = None,
    ) -> RunSummary:
        """Execute onboarding over a CRM CSV/JSON/JSONL dataset."""
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        today = issue_date or date.today()
        today_str = today.strftime("%Y-%m-%d")

        loaded = load_crm_records(input_path)
        for err in loaded.errors:
            self.logger.error(
                f"Row {err.row_index} ({err.client_name}) invalid [{err.field}]: {err.message}"
            )

        store: StateStore | None = None
        if not dry_run or self.settings.db_path.exists():
            store = StateStore(self.settings.db_path)
            if not dry_run:
                store.seed_invoice_sequences(loaded.records)
                store.record_run_start(
                    run_id=run_id,
                    started_at=started_at,
                    mode=mode,
                    dry_run=False,
                    input_source=str(input_path),
                    total_rows=len(loaded.records) + len(loaded.errors),
                )

        # Track dry-run sequence numbers in-memory without writing to SQLite.
        dry_seq_offsets: dict[str, int] = defaultdict(int)
        csv_max_seq_by_date: dict[str, int] = defaultdict(int)
        for rec in loaded.records:
            m = _INV_PATTERN.match(rec.invoice_number.strip().upper())
            if m:
                ymd, seq = m.group(1), int(m.group(2))
                csv_max_seq_by_date[ymd] = max(csv_max_seq_by_date[ymd], seq)

        outcomes: list[ClientOnboardingOutcome] = []
        revenue_totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
        onboarded_count = 0
        partial_count = 0
        skipped_count = 0
        failed_count = len(loaded.errors)
        dry_run_count = 0

        for client in loaded.records:
            if client_filter and client.client_name.strip().lower() != client_filter.strip().lower():
                continue

            if not client.is_won:
                skipped_count += 1
                outcomes.append(
                    ClientOnboardingOutcome(
                        client_name=client.client_name,
                        idempotency_key=client.idempotency_key,
                        status="skipped",
                        reason=f"status={client.status}",
                    )
                )
                continue

            existing_db = store.get_client(client.idempotency_key) if store else None
            already_done = (client.onboarded or existing_db is not None) and not force
            if already_done:
                skipped_count += 1
                reason = "already_onboarded_db" if existing_db is not None else "already_onboarded_input"
                self.logger.info(f"Skipping '{client.client_name}' ({reason}).")
                outcomes.append(
                    ClientOnboardingOutcome(
                        client_name=client.client_name,
                        idempotency_key=client.idempotency_key,
                        status="skipped",
                        reason=reason,
                    )
                )
                continue

            if dry_run:
                outcome = self._simulate_dry_run_client(
                    client=client,
                    mode=mode,
                    today=today,
                    store=store,
                    dry_seq_offsets=dry_seq_offsets,
                    csv_max_seq_by_date=csv_max_seq_by_date,
                    existing_db=existing_db,
                )
                outcomes.append(outcome)
                dry_run_count += 1
                if outcome.invoice:
                    revenue_totals[outcome.invoice.currency_code] += outcome.invoice.total
                continue

            assert store is not None
            outcome = self._process_single_client(
                client=client,
                run_id=run_id,
                mode=mode,
                today=today,
                today_str=today_str,
                store=store,
                existing_db=existing_db,
                send_emails=send_emails,
                attach_invoice=attach_invoice,
                notify_webhook=notify_webhook,
            )
            outcomes.append(outcome)
            if outcome.status == "onboarded":
                onboarded_count += 1
            elif outcome.status == "partial":
                partial_count += 1
            else:
                failed_count += 1

            if outcome.invoice:
                revenue_totals[outcome.invoice.currency_code] += outcome.invoice.total

        finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        summary = RunSummary(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            mode=mode,
            dry_run=dry_run,
            input_source=str(input_path),
            total_rows=len(loaded.records) + len(loaded.errors),
            validation_errors=loaded.errors,
            onboarded_count=onboarded_count,
            partial_count=partial_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            dry_run_count=dry_run_count,
            total_revenue_by_currency={k: f"{v:.2f}" for k, v in sorted(revenue_totals.items())},
            outcomes=outcomes,
        )

        if not dry_run and store is not None:
            run_status = "completed" if failed_count == 0 and partial_count == 0 else (
                "completed_with_warnings" if (onboarded_count + partial_count) > 0 or failed_count == 0 else "failed"
            )
            store.record_run_finish(
                run_id=run_id,
                finished_at=finished_at,
                onboarded_count=onboarded_count,
                partial_count=partial_count,
                skipped_count=skipped_count,
                failed_count=failed_count,
                status=run_status,
            )

            out_ext = ".json" if loaded.source_format in {"json", "jsonl"} else ".csv"
            updated_tracker = self.settings.output_dir / f"clients_updated{out_ext}"
            export_updated_records(updated_tracker, loaded.records, loaded.fieldnames)
            if in_place:
                export_updated_records(input_path, loaded.records, loaded.fieldnames)

            write_run_report(summary=summary, store=store, settings=self.settings)
            self.logger.info(
                f"Run {run_id} finished: onboarded={onboarded_count}, partial={partial_count}, "
                f"skipped={skipped_count}, failed={failed_count}. Tracker -> {updated_tracker}"
            )

        return summary

    def _simulate_dry_run_client(
        self,
        *,
        client: ClientRecord,
        mode: str,
        today: date,
        store: StateStore | None,
        dry_seq_offsets: dict[str, int],
        csv_max_seq_by_date: dict[str, int],
        existing_db: dict[str, Any] | None,
    ) -> ClientOnboardingOutcome:
        ymd = today.strftime("%Y%m%d")
        prefix = self.settings.agency.invoice_prefix
        if store is not None:
            base_preview = store.preview_next_invoice_number(prefix, today)
            base_seq = int(base_preview.rsplit("-", 1)[-1]) - 1
        else:
            base_seq = 0
        effective_base = max(base_seq, csv_max_seq_by_date.get(ymd, 0))
        dry_seq_offsets[ymd] += 1
        preview_inv_num = f"{prefix}-{ymd}-{effective_base + dry_seq_offsets[ymd]:03d}"

        slug = resolve_workspace_slug(
            client,
            self.settings.clients_dir,
            store=store,
            existing_slug=existing_db.get("workspace_slug") if existing_db else None,
        )
        planned_dir = self.settings.clients_dir / slug
        inv_result = generate_invoice(
            client=client,
            invoices_dir=planned_dir / "01_Contracts_and_Invoices",
            invoice_number=preview_inv_num,
            settings=self.settings,
            issue_date=today,
            write_files=False,
        )
        self.logger.info(
            f"[DRY-RUN] Would onboard '{client.client_name}' ({client.project_name}) -> "
            f"workspace={planned_dir} (mode={mode}), invoice={inv_result.invoice_number} "
            f"({inv_result.currency_symbol}{inv_result.total:,.2f} {inv_result.currency_code})"
        )
        return ClientOnboardingOutcome(
            client_name=client.client_name,
            idempotency_key=client.idempotency_key,
            status="dry_run",
            reason="dry_run_preview",
            workspace_location=str(planned_dir),
            workspace_mode=mode,
            invoice=inv_result,
            channels=[ChannelStatus(channel="dry_run", status="dry_run", detail="Preview only")],
        )

    def _process_single_client(
        self,
        *,
        client: ClientRecord,
        run_id: str,
        mode: str,
        today: date,
        today_str: str,
        store: StateStore,
        existing_db: dict[str, Any] | None,
        send_emails: bool,
        attach_invoice: bool,
        notify_webhook: bool,
    ) -> ClientOnboardingOutcome:
        channels: list[ChannelStatus] = []
        self.logger.info(f"Onboarding '{client.client_name}' ({client.project_name})...")

        try:
            slug = resolve_workspace_slug(
                client,
                self.settings.clients_dir,
                store=store,
                existing_slug=existing_db.get("workspace_slug") if existing_db else None,
            )
            paths = create_local_workspace(
                client,
                self.settings,
                slug=slug,
                start_date=client.start_date or today,
            )
            channels.append(
                ChannelStatus(channel="workspace_local", status="success", external_ref=str(paths["root"]))
            )

            workspace_loc = str(paths["root"])
            drive_url = ""
            drive_failed = False

            if mode == "gdrive":
                try:
                    drive_url = create_google_drive_workspace(client, self.settings)
                    workspace_loc = drive_url
                    channels.append(
                        ChannelStatus(channel="workspace_gdrive", status="success", external_ref=drive_url)
                    )
                except Exception as exc:
                    drive_failed = True
                    self.logger.error(f"Google Drive provisioning failed for '{client.client_name}': {exc}")
                    channels.append(
                        ChannelStatus(channel="workspace_gdrive", status="failed", detail=str(exc))
                    )

            invoice_number = store.allocate_invoice_number(self.settings.agency.invoice_prefix, today)
            invoices_dir = paths.get("01_Contracts_and_Invoices", paths["root"] / "01_Contracts_and_Invoices")
            invoice = generate_invoice(
                client=client,
                invoices_dir=invoices_dir,
                invoice_number=invoice_number,
                settings=self.settings,
                issue_date=today,
                write_files=True,
            )
            channels.append(
                ChannelStatus(channel="invoice", status="success", external_ref=str(invoice.html_path or ""))
            )

            draft_txt, _ = write_welcome_email_draft(
                client=client,
                workspace_location=workspace_loc,
                invoice=invoice,
                output_dir=paths["root"],
                settings=self.settings,
            )
            channels.append(
                ChannelStatus(channel="email_draft", status="success", external_ref=str(draft_txt))
            )

            email_failed = False
            if send_emails:
                if not client.email:
                    email_failed = True
                    channels.append(
                        ChannelStatus(channel="email_smtp", status="failed", detail="Missing client email")
                    )
                else:
                    try:
                        subject, plain_body, html_body = render_welcome_email(
                            client, workspace_loc, invoice, self.settings
                        )
                        attachments = [invoice.html_path] if (attach_invoice and invoice.html_path) else None
                        attempts = send_email_smtp(
                            to_email=client.email,
                            subject=subject,
                            plain_body=plain_body,
                            html_body=html_body,
                            settings=self.settings,
                            attachments=attachments,
                        )
                        channels.append(
                            ChannelStatus(
                                channel="email_smtp",
                                status="success",
                                attempts=attempts,
                                external_ref=client.email,
                            )
                        )
                    except Exception as exc:
                        email_failed = True
                        self.logger.error(f"Email delivery failed for '{client.client_name}': {exc}")
                        channels.append(
                            ChannelStatus(channel="email_smtp", status="failed", detail=str(exc))
                        )
            else:
                channels.append(ChannelStatus(channel="email_smtp", status="skipped", detail="--send-emails off"))

            webhook_failed = False
            if notify_webhook:
                try:
                    total_display = f"{invoice.currency_symbol}{invoice.total:,.2f} {invoice.currency_code}"
                    attempts, ref = send_webhook_alert(
                        client_name=client.client_name,
                        project_name=client.project_name,
                        invoice_number=invoice.invoice_number,
                        total_display=total_display,
                        workspace_location=workspace_loc,
                        settings=self.settings,
                    )
                    channels.append(
                        ChannelStatus(channel="webhook", status="success", attempts=attempts, external_ref=ref)
                    )
                except Exception as exc:
                    webhook_failed = True
                    self.logger.error(f"Webhook delivery failed for '{client.client_name}': {exc}")
                    channels.append(
                        ChannelStatus(channel="webhook", status="failed", detail=str(exc))
                    )
            else:
                channels.append(ChannelStatus(channel="webhook", status="skipped", detail="--notify-webhook off"))

            final_status = "partial" if (drive_failed or email_failed or webhook_failed) else "completed"
            store.upsert_client(
                record=client,
                workspace_slug=slug,
                workspace_path=str(paths["root"]),
                workspace_mode=mode,
                drive_url=drive_url,
                invoice_number=invoice.invoice_number,
                currency_code=invoice.currency_code,
                subtotal=f"{invoice.subtotal:.2f}",
                discount_amount=f"{invoice.discount_amount:.2f}",
                tax_amount=f"{invoice.tax_amount:.2f}",
                total_amount=f"{invoice.total:.2f}",
                onboarded_date=today_str,
                status=final_status,
            )
            for ch in channels:
                store.record_delivery_event(
                    idempotency_key=client.idempotency_key,
                    run_id=run_id,
                    channel=ch.channel,
                    status=ch.status,
                    attempts=ch.attempts,
                    last_error=ch.detail,
                    external_ref=ch.external_ref,
                )

            client.onboarded = True
            client.onboarded_date = today_str
            client.invoice_number = invoice.invoice_number

            return ClientOnboardingOutcome(
                client_name=client.client_name,
                idempotency_key=client.idempotency_key,
                status="partial" if final_status == "partial" else "onboarded",
                workspace_location=workspace_loc,
                workspace_mode=mode,
                invoice=invoice,
                channels=channels,
            )

        except Exception as exc:
            self.logger.exception(f"Fatal onboarding error for '{client.client_name}': {exc}")
            return ClientOnboardingOutcome(
                client_name=client.client_name,
                idempotency_key=client.idempotency_key,
                status="failed",
                error=str(exc),
                channels=channels,
            )

    def retry_failed(
        self,
        *,
        channel_filter: str = "all",
        attach_invoice: bool = False,
    ) -> dict[str, int]:
        """Replay failed email_smtp and/or webhook delivery events from SQLite without re-invoicing."""
        store = StateStore(self.settings.db_path)
        allowed = {"email_smtp", "webhook"} if channel_filter == "all" else {channel_filter}
        failed_items = store.get_failed_deliveries(channels=allowed)
        run_id = "retry-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        succeeded = 0
        still_failed = 0

        for item in failed_items:
            key = str(item["idempotency_key"])
            channel = str(item["channel"])
            client_name = str(item["client_name"])
            workspace_loc = str(item["drive_url"] or item["workspace_path"])
            invoice_number = str(item["invoice_number"])

            if channel == "email_smtp":
                try:
                    ws_root = Path(str(item["workspace_path"]))
                    draft_txt = ws_root / "welcome_email_draft.txt"
                    draft_html = ws_root / "welcome_email_draft.html"
                    if draft_txt.exists():
                        raw_txt = draft_txt.read_text(encoding="utf-8")
                        lines = raw_txt.splitlines()
                        subj_line = next((l for l in lines if l.startswith("Subject:")), f"Subject: Welcome, {client_name}!")
                        subject = subj_line.replace("Subject:", "", 1).strip()
                        plain_body = "\n".join(l for l in lines if not l.startswith(("To:", "Subject:"))).strip()
                    else:
                        subject = f"Welcome to {self.settings.agency.agency_name}, {client_name}!"
                        plain_body = f"Hi {item['contact_name'] or client_name}, your workspace is ready: {workspace_loc}"

                    html_body = draft_html.read_text(encoding="utf-8") if draft_html.exists() else f"<p>{plain_body}</p>"
                    attachments = None
                    if attach_invoice:
                        inv_html = ws_root / "01_Contracts_and_Invoices" / f"{invoice_number}.html"
                        if inv_html.exists():
                            attachments = [inv_html]

                    attempts = send_email_smtp(
                        to_email=str(item["email"]),
                        subject=subject,
                        plain_body=plain_body,
                        html_body=html_body,
                        settings=self.settings,
                        attachments=attachments,
                    )
                    store.record_delivery_event(
                        idempotency_key=key,
                        run_id=run_id,
                        channel="email_smtp",
                        status="success",
                        attempts=attempts,
                        external_ref=str(item["email"]),
                    )
                    succeeded += 1
                    self.logger.info(f"Retry succeeded [email_smtp] for '{client_name}'.")
                except Exception as exc:
                    still_failed += 1
                    store.record_delivery_event(
                        idempotency_key=key,
                        run_id=run_id,
                        channel="email_smtp",
                        status="failed",
                        attempts=int(item["attempts"]) + 1,
                        last_error=str(exc),
                    )
                    self.logger.error(f"Retry failed [email_smtp] for '{client_name}': {exc}")

            elif channel == "webhook":
                try:
                    curr = str(item["currency_code"] or self.settings.agency.currency_code)
                    total_display = f"{item['total_amount']} {curr}"
                    attempts, ref = send_webhook_alert(
                        client_name=client_name,
                        project_name=str(item["project_name"]),
                        invoice_number=invoice_number,
                        total_display=total_display,
                        workspace_location=workspace_loc,
                        settings=self.settings,
                    )
                    store.record_delivery_event(
                        idempotency_key=key,
                        run_id=run_id,
                        channel="webhook",
                        status="success",
                        attempts=attempts,
                        external_ref=ref,
                    )
                    succeeded += 1
                    self.logger.info(f"Retry succeeded [webhook] for '{client_name}'.")
                except Exception as exc:
                    still_failed += 1
                    store.record_delivery_event(
                        idempotency_key=key,
                        run_id=run_id,
                        channel="webhook",
                        status="failed",
                        attempts=int(item["attempts"]) + 1,
                        last_error=str(exc),
                    )
                    self.logger.error(f"Retry failed [webhook] for '{client_name}': {exc}")

            remaining_failures = [
                ev for ev in store.get_delivery_events_for_client(key) if ev["status"] == "failed"
            ]
            if not remaining_failures:
                store.update_client_status(key, "completed")

        return {"retried": len(failed_items), "succeeded": succeeded, "failed": still_failed}
