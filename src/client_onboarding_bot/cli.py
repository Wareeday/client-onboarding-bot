"""Command-Line Interface for the Multi-Channel Client Onboarding Bot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from client_onboarding_bot import __version__
from client_onboarding_bot.config import load_settings, validate_settings
from client_onboarding_bot.ingestion import load_crm_records
from client_onboarding_bot.logging_utils import configure_logging
from client_onboarding_bot.pipeline import OnboardingPipeline
from client_onboarding_bot.reporting import generate_dashboard_from_store
from client_onboarding_bot.state_store import StateStore

_SUBCOMMANDS = {"onboard", "status", "retry-failed", "validate", "report"}


def _add_common_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None, help="Path to YAML/JSON/TOML config override file.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Override base output directory (default: output/).")
    parser.add_argument("--db-path", type=Path, default=None, help="Override SQLite state DB path.")
    parser.add_argument("--json-logs", action="store_true", help="Emit structured JSON logs to stdout.")


def _add_onboard_args(parser: argparse.ArgumentParser) -> None:
    _add_common_config_args(parser)
    parser.add_argument(
        "--input-csv",
        "--input",
        dest="input_file",
        type=Path,
        default=None,
        help="Path to CRM CSV, JSON, or JSONL file (default: sample_data/clients_sample.csv).",
    )
    parser.add_argument(
        "--mode",
        choices=["local", "gdrive"],
        default="local",
        help="Workspace provisioning mode: 'local' (zero credentials) or 'gdrive' (Google Drive API).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview onboarding plan without modifying files, DB, or external services.")
    parser.add_argument("--send-emails", action="store_true", help="Send welcome emails via configured Gmail/SMTP server.")
    parser.add_argument("--attach-invoice", action="store_true", help="Attach HTML invoice when sending welcome emails.")
    parser.add_argument("--notify-webhook", action="store_true", help="Send rich Slack/Discord/JSON alert to WEBHOOK_URL.")
    parser.add_argument("--client", dest="client_filter", default=None, help="Process only the matching Client Name.")
    parser.add_argument("--force", action="store_true", help="Re-onboard matching Won clients even if already recorded.")
    parser.add_argument("--in-place", action="store_true", help="Also write updated Onboarded/Invoice columns back to input file.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client-onboarding-bot",
        description=(
            "Production-grade client onboarding engine: SQLite idempotency, Decimal invoicing, "
            "structured workspaces, multipart SMTP, and Slack/Discord webhooks."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    onboard_p = subparsers.add_parser("onboard", help="Onboard newly Won clients from CSV/JSON/JSONL (default command).")
    _add_onboard_args(onboard_p)

    status_p = subparsers.add_parser("status", help="Inspect onboarded client ledger and channel delivery status.")
    _add_common_config_args(status_p)
    status_p.add_argument("--status", dest="status_filter", choices=["completed", "partial", "failed"], default=None)
    status_p.add_argument("--limit", type=int, default=50)
    status_p.add_argument("--json", dest="as_json", action="store_true", help="Print machine-readable JSON output.")

    retry_p = subparsers.add_parser("retry-failed", help="Retry failed SMTP or webhook notifications from SQLite state.")
    _add_common_config_args(retry_p)
    retry_p.add_argument("--channel", choices=["all", "email_smtp", "webhook"], default="all")
    retry_p.add_argument("--attach-invoice", action="store_true")

    val_p = subparsers.add_parser("validate", help="Validate CRM input schema and configuration without onboarding.")
    _add_common_config_args(val_p)
    val_p.add_argument("--input-csv", "--input", dest="input_file", type=Path, default=None)
    val_p.add_argument("--mode", choices=["local", "gdrive"], default="local")
    val_p.add_argument("--send-emails", action="store_true")
    val_p.add_argument("--notify-webhook", action="store_true")

    rep_p = subparsers.add_parser("report", help="Regenerate HTML executive onboarding dashboard from SQLite ledger.")
    _add_common_config_args(rep_p)

    return parser


def _normalize_argv(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    if not args:
        return ["onboard"]
    if args[0] in _SUBCOMMANDS or args[0] in {"-h", "--help", "--version"}:
        return args
    # Backward compatibility: allow `python3 main.py --dry-run --input-csv ...` without typing `onboard`.
    return ["onboard", *args]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    normalized = _normalize_argv(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(normalized)

    command = args.command or "onboard"
    settings = load_settings(
        config_file=getattr(args, "config", None),
        output_dir=getattr(args, "output_dir", None),
        db_path=getattr(args, "db_path", None),
    )

    is_dry_run = bool(getattr(args, "dry_run", False))
    log_dir = None if (is_dry_run or command in {"status", "validate"}) else settings.log_dir
    logger = configure_logging(
        log_dir=log_dir,
        level=settings.log_level,
        json_logs=bool(getattr(args, "json_logs", False)),
    )

    if command == "onboard":
        input_path = args.input_file or (settings.base_dir / "sample_data" / "clients_sample.csv")
        cfg_errors = validate_settings(
            settings,
            mode=args.mode,
            send_emails=args.send_emails,
            notify_webhook=args.notify_webhook,
        )
        if cfg_errors:
            for err in cfg_errors:
                logger.error(f"Configuration error: {err}")
            return 2

        pipeline = OnboardingPipeline(settings, logger)
        summary = pipeline.run(
            input_path,
            mode=args.mode,
            dry_run=args.dry_run,
            send_emails=args.send_emails,
            attach_invoice=args.attach_invoice,
            notify_webhook=args.notify_webhook,
            client_filter=args.client_filter,
            force=args.force,
            in_place=args.in_place,
        )
        if not getattr(args, "json_logs", False):
            if summary.dry_run:
                print(
                    f"\n[DRY-RUN] Preview complete: {summary.dry_run_count} client(s) would be onboarded, "
                    f"{summary.skipped_count} skipped, {summary.failed_count} validation error(s)."
                )
            else:
                print(
                    f"\nDone! Onboarded={summary.onboarded_count}, Partial={summary.partial_count}, "
                    f"Skipped={summary.skipped_count}, Failed={summary.failed_count}."
                )
                print(f"State DB     -> {settings.db_path}")
                print(f"Workspaces   -> {settings.clients_dir}")
                print(f"Dashboard    -> {settings.reports_dir / 'onboarding_dashboard.html'}")
        return 1 if summary.failed_count > 0 or summary.partial_count > 0 else 0

    if command == "validate":
        input_path = args.input_file or (settings.base_dir / "sample_data" / "clients_sample.csv")
        cfg_errors = validate_settings(
            settings,
            mode=args.mode,
            send_emails=args.send_emails,
            notify_webhook=args.notify_webhook,
        )
        loaded = load_crm_records(input_path)
        for err in cfg_errors:
            logger.error(f"Config error: {err}")
        for r_err in loaded.errors:
            logger.error(f"Row {r_err.row_index} ({r_err.client_name}) [{r_err.field}]: {r_err.message}")

        total_errors = len(cfg_errors) + len(loaded.errors)
        won_ready = sum(1 for r in loaded.records if r.is_won and not r.onboarded)
        print(
            f"Validation {'PASSED' if total_errors == 0 else 'FAILED'}: "
            f"{len(loaded.records)} valid row(s) ({won_ready} Won & pending), {total_errors} error(s)."
        )
        return 0 if total_errors == 0 else 1

    if command == "status":
        if not settings.db_path.exists():
            if args.as_json:
                print(json.dumps({"clients": [], "runs": []}))
            else:
                print(f"No onboarding ledger found at {settings.db_path}. Run `python3 main.py` first.")
            return 0
        store = StateStore(settings.db_path)
        clients = store.list_clients(status_filter=args.status_filter, limit=args.limit)
        runs = store.list_runs(limit=5)
        if args.as_json:
            print(json.dumps({"clients": clients, "runs": runs}, indent=2))
            return 0
        if not clients:
            print("No onboarded clients match the query.")
            return 0
        print(f"{'Client Name':<28} {'Invoice':<18} {'Total':<14} {'Status':<10} {'Date':<12}")
        print("-" * 86)
        for c in clients:
            total_str = f"{c['currency_code']} {c['total_amount']}"
            print(
                f"{c['client_name'][:27]:<28} {c['invoice_number']:<18} {total_str:<14} "
                f"{c['status']:<10} {c['onboarded_date']:<12}"
            )
        return 0

    if command == "retry-failed":
        if not settings.db_path.exists():
            logger.error(f"No SQLite ledger found at {settings.db_path}.")
            return 1
        store = StateStore(settings.db_path)
        pending = store.get_failed_deliveries(
            channels={"email_smtp", "webhook"} if args.channel == "all" else {args.channel}
        )
        if not pending:
            print("No failed deliveries found to retry.")
            return 0
        need_email = any(p["channel"] == "email_smtp" for p in pending)
        need_webhook = any(p["channel"] == "webhook" for p in pending)
        cfg_errors = validate_settings(settings, send_emails=need_email, notify_webhook=need_webhook)
        if cfg_errors:
            for err in cfg_errors:
                logger.error(f"Configuration error: {err}")
            return 2

        pipeline = OnboardingPipeline(settings, logger)
        result = pipeline.retry_failed(channel_filter=args.channel, attach_invoice=args.attach_invoice)
        print(
            f"Retry summary: retried={result['retried']}, succeeded={result['succeeded']}, "
            f"failed={result['failed']}"
        )
        return 0 if result["failed"] == 0 else 1

    if command == "report":
        if not settings.db_path.exists():
            logger.error(f"No SQLite ledger found at {settings.db_path}. Run onboarding first.")
            return 1
        store = StateStore(settings.db_path)
        dash_path = generate_dashboard_from_store(store=store, settings=settings)
        print(f"Generated onboarding dashboard -> {dash_path}")
        return 0

    parser.print_help()
    return 2
