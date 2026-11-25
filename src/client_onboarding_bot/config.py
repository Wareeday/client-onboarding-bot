"""Layered configuration loader and validator (.env, YAML/JSON/TOML, root config.py, and CLI)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from client_onboarding_bot.models import MilestoneSpec

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULT_SUBFOLDERS = [
    "01_Contracts_and_Invoices",
    "02_Client_Assets",
    "03_Automation_Deliverables",
    "04_Weekly_Reports",
]

DEFAULT_MILESTONES_LIST = [
    MilestoneSpec("Kickoff Call & Access Setup", 1, "Not Started"),
    MilestoneSpec("Architecture & Prototype Demo", 5, "Not Started"),
    MilestoneSpec("Testing on Sample Data", 9, "Not Started"),
    MilestoneSpec("Final Handover & Documentation", 14, "Not Started"),
]

DEFAULT_WELCOME_TEMPLATE = """Subject: Welcome to {agency_name}, {client_name}! (Kickoff & Next Steps)

Hi {contact_name},

We're excited to kick off the **{project_name}** engagement for **{client_name}**!

To keep everything organized from day one, your onboarding workspace and billing package are ready:
- **Client Folder:** {workspace_location}
- **Project Tracker:** Included inside your folder with our milestone schedule
- **Invoice Reference:** {invoice_number} (Total: {currency}{total_amount:.2f}, due {due_date})

**Quick next steps for you:**
1. Upload any sample spreadsheets, brand assets, or docs into `02_Client_Assets`.
2. Reply to this email with your preferred time for our 20-minute kickoff call.

Looking forward to building this with you!

Best regards,
{sender_name}
{agency_name}
"""


@dataclass
class AgencySettings:
    agency_name: str = "Your Automation Studio"
    sender_name: str = "Your Name"
    sender_email: str = "you@example.com"
    agency_address: str = ""
    tax_id: str = ""
    website: str = ""
    payment_instructions: str = "Bank transfer / Wise — settlement details provided upon kickoff."
    currency_symbol: str = "$"
    currency_code: str = "USD"
    tax_label: str = "Tax"
    tax_rate: Decimal = Decimal("0.00")
    invoice_due_days: int = 14
    invoice_prefix: str = "INV"


@dataclass
class RetrySettings:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    backoff_multiplier: float = 2.0


@dataclass
class SMTPSettings:
    host: str = ""
    port: int = 465
    username: str = ""
    password: str = ""
    use_tls: bool = False
    use_ssl: bool = True
    from_address: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.username and self.password)


@dataclass
class WebhookSettings:
    url: str = ""
    fmt: str = "auto"  # auto | slack | discord | generic


@dataclass
class DriveSettings:
    credentials_path: str = "credentials.json"
    parent_folder_id: str = ""


@dataclass
class PipelineSettings:
    base_dir: Path
    output_dir: Path
    clients_dir: Path
    db_path: Path
    log_dir: Path
    reports_dir: Path
    agency: AgencySettings = field(default_factory=AgencySettings)
    subfolders: list[str] = field(default_factory=lambda: list(DEFAULT_SUBFOLDERS))
    milestones: list[MilestoneSpec] = field(default_factory=lambda: list(DEFAULT_MILESTONES_LIST))
    welcome_email_template: str = DEFAULT_WELCOME_TEMPLATE
    retry: RetrySettings = field(default_factory=RetrySettings)
    smtp: SMTPSettings = field(default_factory=SMTPSettings)
    webhook: WebhookSettings = field(default_factory=WebhookSettings)
    drive: DriveSettings = field(default_factory=DriveSettings)
    log_level: str = "INFO"


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return default


def load_env_file(env_path: Path | None = None) -> None:
    """Load `.env` via python-dotenv if installed, otherwise via built-in stdlib parser."""
    target = env_path or (Path.cwd() / ".env")
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=target if target.exists() else None)
        return
    except ImportError:
        pass

    if not target.exists():
        return

    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] in {'"', "'"}:
            quote_char = val[0]
            end_idx = val.find(quote_char, 1)
            if end_idx != -1:
                val = val[1:end_idx]
        elif "#" in val:
            val = val.split("#", 1)[0].strip()
        os.environ.setdefault(key, val)


def _find_project_root() -> Path:
    cwd_config = Path.cwd() / "config.py"
    if cwd_config.exists():
        return Path.cwd()
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / "config.py").exists():
        return pkg_root
    return Path.cwd()


def _load_root_config_module(base_dir: Path) -> Any | None:
    config_py = base_dir / "config.py"
    if not config_py.exists():
        return None
    spec = importlib.util.spec_from_file_location("_onboarding_root_config", config_py)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_structured_config_file(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    suffix = config_path.suffix.lower()
    text = config_path.read_text(encoding="utf-8")
    if suffix == ".json":
        data = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required to load YAML configuration files. "
                "Install it (`pip install PyYAML`) or use a .json/.toml config file."
            ) from exc
        data = yaml.safe_load(text) or {}
    elif suffix == ".toml":
        if tomllib is None:
            raise RuntimeError("TOML config files require Python 3.11+.")
        data = tomllib.loads(text)
    else:
        raise ValueError(f"Unsupported config format '{suffix}'. Use .yaml, .yml, .json, or .toml.")
    if not isinstance(data, dict):
        raise ValueError(f"Top-level config in {config_path} must be a mapping/object.")
    return data


def _coerce_decimal(value: Any, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal value '{value}'") from exc


def _parse_milestones(raw_items: Any) -> list[MilestoneSpec]:
    specs: list[MilestoneSpec] = []
    for item in raw_items:
        if isinstance(item, MilestoneSpec):
            specs.append(item)
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            name = str(item[0])
            offset = int(item[1])
            status = str(item[2]) if len(item) >= 3 else "Not Started"
            notes = str(item[3]) if len(item) >= 4 else ""
            specs.append(MilestoneSpec(name=name, day_offset=offset, status=status, notes=notes))
        elif isinstance(item, dict):
            specs.append(
                MilestoneSpec(
                    name=str(item["name"]),
                    day_offset=int(item.get("day_offset", 1)),
                    status=str(item.get("status", "Not Started")),
                    notes=str(item.get("notes", "")),
                )
            )
    return specs


def load_settings(
    *,
    base_dir: Path | None = None,
    config_file: Path | None = None,
    output_dir: Path | None = None,
    db_path: Path | None = None,
    env_file: Path | None = None,
) -> PipelineSettings:
    """Resolve layered configuration from root config.py, YAML/JSON/TOML, .env, and overrides."""
    root_dir = (base_dir or _find_project_root()).resolve()
    load_env_file(env_file or (root_dir / ".env"))

    root_mod = _load_root_config_module(root_dir)
    resolved_output = Path(
        output_dir
        or os.getenv("ONBOARDING_OUTPUT_DIR")
        or (getattr(root_mod, "OUTPUT_DIR", root_dir / "output"))
    )
    if not resolved_output.is_absolute():
        resolved_output = (root_dir / resolved_output).resolve()

    resolved_db = Path(
        db_path
        or os.getenv("ONBOARDING_DB_PATH")
        or (resolved_output / "state" / "onboarding.db")
    )
    if not resolved_db.is_absolute():
        resolved_db = (root_dir / resolved_db).resolve()

    agency = AgencySettings()
    subfolders = list(DEFAULT_SUBFOLDERS)
    milestones = list(DEFAULT_MILESTONES_LIST)
    welcome_template = DEFAULT_WELCOME_TEMPLATE
    retry = RetrySettings()

    if root_mod is not None:
        info = getattr(root_mod, "AGENCY_INFO", {}) or {}
        agency = AgencySettings(
            agency_name=str(info.get("agency_name", agency.agency_name)),
            sender_name=str(info.get("sender_name", agency.sender_name)),
            sender_email=str(info.get("sender_email", agency.sender_email)),
            agency_address=str(info.get("agency_address", agency.agency_address)),
            tax_id=str(info.get("tax_id", agency.tax_id)),
            website=str(info.get("website", agency.website)),
            payment_instructions=str(info.get("payment_instructions", agency.payment_instructions)),
            currency_symbol=str(info.get("currency_symbol", agency.currency_symbol)),
            currency_code=str(info.get("currency_code", agency.currency_code)),
            tax_label=str(info.get("tax_label", agency.tax_label)),
            tax_rate=_coerce_decimal(info.get("tax_rate"), agency.tax_rate),
            invoice_due_days=int(info.get("invoice_due_days", agency.invoice_due_days)),
            invoice_prefix=str(info.get("invoice_prefix", agency.invoice_prefix)),
        )
        if hasattr(root_mod, "CLIENT_SUBFOLDERS"):
            subfolders = [str(x) for x in root_mod.CLIENT_SUBFOLDERS]
        if hasattr(root_mod, "DEFAULT_MILESTONES"):
            milestones = _parse_milestones(root_mod.DEFAULT_MILESTONES)
        if hasattr(root_mod, "WELCOME_EMAIL_TEMPLATE"):
            welcome_template = str(root_mod.WELCOME_EMAIL_TEMPLATE)

    # Auto-detect config.yaml / config.json if --config and ONBOARDING_CONFIG were omitted.
    cfg_candidate = config_file
    if cfg_candidate is None and os.getenv("ONBOARDING_CONFIG"):
        cfg_candidate = Path(os.environ["ONBOARDING_CONFIG"])
    if cfg_candidate is None:
        for default_name in ("config.yaml", "config.yml", "config.json"):
            candidate = root_dir / default_name
            if candidate.exists():
                cfg_candidate = candidate
                break

    if cfg_candidate is not None:
        raw_cfg = _load_structured_config_file(cfg_candidate)
        if isinstance(raw_cfg.get("agency"), dict):
            a = raw_cfg["agency"]
            agency.agency_name = str(a.get("agency_name", agency.agency_name))
            agency.sender_name = str(a.get("sender_name", agency.sender_name))
            agency.sender_email = str(a.get("sender_email", agency.sender_email))
            agency.agency_address = str(a.get("agency_address", agency.agency_address))
            agency.tax_id = str(a.get("tax_id", agency.tax_id))
            agency.website = str(a.get("website", agency.website))
            agency.payment_instructions = str(a.get("payment_instructions", agency.payment_instructions))
            agency.currency_symbol = str(a.get("currency_symbol", agency.currency_symbol))
            agency.currency_code = str(a.get("currency_code", agency.currency_code))
            agency.tax_label = str(a.get("tax_label", agency.tax_label))
            agency.tax_rate = _coerce_decimal(a.get("tax_rate"), agency.tax_rate)
            agency.invoice_due_days = int(a.get("invoice_due_days", agency.invoice_due_days))
            agency.invoice_prefix = str(a.get("invoice_prefix", agency.invoice_prefix))
        if isinstance(raw_cfg.get("subfolders"), list):
            subfolders = [str(s) for s in raw_cfg["subfolders"]]
        if isinstance(raw_cfg.get("milestones"), list):
            milestones = _parse_milestones(raw_cfg["milestones"])
        if isinstance(raw_cfg.get("welcome_email_template"), str):
            welcome_template = raw_cfg["welcome_email_template"]
        if isinstance(raw_cfg.get("retry"), dict):
            r = raw_cfg["retry"]
            retry = RetrySettings(
                max_attempts=int(r.get("max_attempts", retry.max_attempts)),
                base_delay_seconds=float(r.get("base_delay_seconds", retry.base_delay_seconds)),
                backoff_multiplier=float(r.get("backoff_multiplier", retry.backoff_multiplier)),
            )

    # Resolve SMTP settings (Generic SMTP_HOST takes precedence over Gmail defaults).
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    gmail_user = os.getenv("GMAIL_ADDRESS", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    if smtp_host:
        smtp = SMTPSettings(
            host=smtp_host,
            port=int(os.getenv("SMTP_PORT", "587")),
            username=os.getenv("SMTP_USERNAME", gmail_user).strip(),
            password=os.getenv("SMTP_PASSWORD", gmail_pass).strip(),
            use_tls=_parse_bool(os.getenv("SMTP_USE_TLS"), default=True),
            use_ssl=_parse_bool(os.getenv("SMTP_USE_SSL"), default=False),
            from_address=os.getenv("SMTP_FROM", "").strip() or os.getenv("SMTP_USERNAME", gmail_user).strip(),
        )
    else:
        smtp = SMTPSettings(
            host="smtp.gmail.com" if (gmail_user and gmail_pass) else "",
            port=465,
            username=gmail_user,
            password=gmail_pass,
            use_tls=False,
            use_ssl=True,
            from_address=gmail_user,
        )

    webhook = WebhookSettings(
        url=os.getenv("WEBHOOK_URL", "").strip(),
        fmt=os.getenv("WEBHOOK_FORMAT", "auto").strip().lower() or "auto",
    )
    drive = DriveSettings(
        credentials_path=os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json").strip(),
        parent_folder_id=os.getenv("GOOGLE_DRIVE_PARENT_FOLDER_ID", "").strip(),
    )

    clients_dir = resolved_output / "clients"
    if output_dir is None and root_mod is not None and hasattr(root_mod, "CLIENTS_OUTPUT_DIR") and not os.getenv("ONBOARDING_OUTPUT_DIR"):
        clients_dir = Path(root_mod.CLIENTS_OUTPUT_DIR)

    return PipelineSettings(
        base_dir=root_dir,
        output_dir=resolved_output,
        clients_dir=clients_dir,
        db_path=resolved_db,
        log_dir=resolved_output / "logs",
        reports_dir=resolved_output / "reports",
        agency=agency,
        subfolders=subfolders,
        milestones=milestones,
        welcome_email_template=welcome_template,
        retry=retry,
        smtp=smtp,
        webhook=webhook,
        drive=drive,
        log_level=os.getenv("ONBOARDING_LOG_LEVEL", "INFO").strip().upper(),
    )


def validate_settings(
    settings: PipelineSettings,
    *,
    mode: str = "local",
    send_emails: bool = False,
    notify_webhook: bool = False,
) -> list[str]:
    """Validate configuration invariants and active integration prerequisites."""
    errors: list[str] = []

    if not settings.agency.agency_name.strip():
        errors.append("agency.agency_name cannot be empty.")
    if settings.agency.sender_email and not _EMAIL_RE.match(settings.agency.sender_email.strip()):
        errors.append(f"Invalid agency.sender_email: '{settings.agency.sender_email}'.")
    if settings.agency.tax_rate < Decimal("0") or settings.agency.tax_rate > Decimal("1"):
        errors.append(f"agency.tax_rate must be between 0.0 and 1.0 (got {settings.agency.tax_rate}).")
    if settings.agency.invoice_due_days < 0:
        errors.append("agency.invoice_due_days must be >= 0.")
    if settings.retry.max_attempts < 1:
        errors.append("retry.max_attempts must be >= 1.")
    if not settings.subfolders:
        errors.append("subfolders cannot be empty.")
    if "01_Contracts_and_Invoices" not in settings.subfolders:
        errors.append("subfolders must include '01_Contracts_and_Invoices' for invoice storage.")

    if mode == "gdrive":
        creds_path = Path(settings.drive.credentials_path)
        if not creds_path.is_absolute():
            creds_path = settings.base_dir / creds_path
        if not creds_path.exists():
            errors.append(f"Google Drive mode requires credentials file at '{creds_path}'.")

    if send_emails and not settings.smtp.is_configured:
        errors.append(
            "Email sending enabled (--send-emails) but SMTP credentials are missing "
            "(set GMAIL_ADDRESS + GMAIL_APP_PASSWORD or SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD)."
        )

    if notify_webhook:
        if not settings.webhook.url:
            errors.append("Webhook notification enabled (--notify-webhook) but WEBHOOK_URL is missing.")
        elif not settings.webhook.url.startswith(("https://", "http://")):
            errors.append(f"WEBHOOK_URL must start with http:// or https:// (got '{settings.webhook.url}').")
        if settings.webhook.fmt not in {"auto", "slack", "discord", "generic"}:
            errors.append(f"Invalid WEBHOOK_FORMAT '{settings.webhook.fmt}'.")

    return errors
