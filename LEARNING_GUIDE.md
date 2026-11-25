# Architecture & Customization Guide

This guide walks through the internal architecture of `client-onboarding-bot`, its idempotency and failure-recovery guarantees, and practical recipes for extending or deploying it in production.

---

## 1. Module Architecture & Data Flow

1. **`src/client_onboarding_bot/config.py` (Layered Configuration):**
   Resolves runtime settings across four layers (`CLI flags` > `Environment / .env` > `YAML/JSON/TOML config` > root `config.py`). Includes a zero-dependency `.env` parser fallback so environment files work even when `python-dotenv` is not installed.

2. **`src/client_onboarding_bot/ingestion.py` (CRM Loader & Validation):**
   Parses `.csv`, `.json`, and `.jsonl` files, converts monetary amounts into exact `Decimal` values, validates email syntax, discounts, tax rates, and ISO dates, and isolates bad rows into `RowValidationError` objects without aborting valid clients.

3. **`src/client_onboarding_bot/state_store.py` (SQLite WAL Ledger):**
   Persists four core tables in `output/state/onboarding.db`:
   - `clients_onboarded`: Stores each client's deterministic `idempotency_key`, workspace slug, invoice totals, and status (`completed` / `partial` / `failed`).
   - `invoice_sequences`: Maintains atomic daily sequence counters (`INV-YYYYMMDD-###`) seeded from existing CRM records so invoice numbers never collide across batches.
   - `delivery_events`: Records per-channel execution status (`workspace_local`, `workspace_gdrive`, `invoice`, `email_draft`, `email_smtp`, `webhook`) to power `retry-failed`.
   - `onboarding_runs`: Audit trail of every batch execution.

4. **`src/client_onboarding_bot/workspace_builder.py` (Local & Google Drive Provisioning):**
   Creates collision-free client folders (`Acme_Corp`, `Acme_Corp_2`), populates `Project_Tracker.csv` with milestone dates, generates `Kickoff_Brief.md`, and optionally provisions idempotent Google Drive folders via Drive API v3 with retry backoff.

5. **`src/client_onboarding_bot/invoice_builder.py` (Financial Engine):**
   Computes subtotal, flat/percentage discounts, and tax/VAT using `Decimal` (`ROUND_HALF_UP`), escaping all user-controlled fields before rendering Markdown and `@media print` HTML5 invoices.

6. **`src/client_onboarding_bot/notifier.py` (Email & Webhook Delivery):**
   Writes `welcome_email_draft.txt` and `welcome_email_draft.html`, sends multipart MIME emails (with optional invoice attachments) via Gmail or custom SMTP, and posts native Slack Block Kit, Discord Embed, or generic JSON webhook alerts with exponential backoff + jitter.

7. **`src/client_onboarding_bot/reporting.py` & `pipeline.py` (Orchestration & Reporting):**
   Coordinates per-client error isolation, `--dry-run` simulation, CRM export updates (`clients_updated.csv`/`.json`), JSON run telemetry (`latest_run.json`), and the offline `onboarding_dashboard.html` report.

---

## 2. Customization Cookbook

### A. Override Agency Profile Without Touching Python Code
Copy `config.example.yaml` to `config.yaml` and update your agency metadata, currency, tax label/rate, folders, and milestones:

```bash
cp config.example.yaml config.yaml
python3 main.py --config config.yaml --dry-run
```

### B. Recovering From Transient Email or Webhook Outages
If an external channel fails mid-batch (e.g., temporary SMTP outage or webhook HTTP 503):
1. The pipeline preserves the client's workspace and invoice and marks the client `partial` in SQLite.
2. Inspect affected records:
   ```bash
   python3 main.py status --status partial
   ```
3. Once credentials or network connectivity are restored, replay only the failed channels:
   ```bash
   python3 main.py retry-failed --attach-invoice
   ```

### C. Scheduled Production Execution (Cron / GitHub Actions / Docker)
Because SQLite idempotency prevents duplicate workspaces, invoices, and emails even when reading the same upstream CRM export repeatedly, you can safely schedule daily runs:

```cron
0 8 * * * cd /opt/client-onboarding-bot && /usr/bin/python3 main.py --input /data/crm_export.csv --send-emails --notify-webhook --json-logs
```

### D. Packaging & Editable Installation
Install locally as an editable package with CLI command `client-onboarding-bot`:

```bash
pip install -e ".[all,dev]"
client-onboarding-bot --help
```
