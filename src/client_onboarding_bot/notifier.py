"""Multi-Channel Notifier: email drafts, multipart SMTP with retries, and Slack/Discord/JSON webhooks."""

from __future__ import annotations

import html
import json
import mimetypes
import random
import smtplib
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, TypeVar

from client_onboarding_bot.config import PipelineSettings, RetrySettings
from client_onboarding_bot.models import ClientRecord, InvoiceResult

_T = TypeVar("_T")


def execute_with_retry(
    operation: Callable[[], _T],
    retry: RetrySettings,
    is_retryable: Callable[[Exception], bool],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[_T, int]:
    """Run an operation with bounded exponential backoff and jitter."""
    attempts = max(1, retry.max_attempts)
    delay = max(0.0, retry.base_delay_seconds)

    for attempt in range(1, attempts + 1):
        try:
            return operation(), attempt
        except Exception as exc:
            retryable = is_retryable(exc)
            if not retryable or attempt >= attempts:
                raise
            retry_after = 0.0
            if isinstance(exc, urllib.error.HTTPError):
                hdr = exc.headers.get("Retry-After") if exc.headers else None
                if hdr and hdr.isdigit():
                    retry_after = min(float(hdr), 30.0)
                exc.close()
            jitter = random.uniform(0.0, delay * 0.25) if delay > 0 else 0.0
            sleep_fn(max(delay + jitter, retry_after))
            delay *= max(1.0, retry.backoff_multiplier)

    raise RuntimeError("Unreachable retry termination")  # pragma: no cover


def render_welcome_email(
    client: ClientRecord,
    workspace_location: str,
    invoice: InvoiceResult,
    settings: PipelineSettings,
) -> tuple[str, str, str]:
    """Render welcome email subject, plain-text body, and clean HTML alternative."""
    rendered = settings.welcome_email_template.format(
        agency_name=settings.agency.agency_name,
        sender_name=settings.agency.sender_name,
        client_name=client.client_name,
        contact_name=client.contact_name or client.client_name,
        project_name=client.project_name,
        workspace_location=workspace_location,
        invoice_number=invoice.invoice_number,
        currency=invoice.currency_symbol,
        total_amount=float(invoice.total),
        due_date=invoice.due_date.strftime("%Y-%m-%d"),
    )

    lines = rendered.splitlines()
    subject = f"Welcome to {settings.agency.agency_name}, {client.client_name}!"
    body_lines: list[str] = []
    for line in lines:
        if line.startswith("Subject:") and len(body_lines) == 0:
            subject = line.replace("Subject:", "", 1).strip()
            continue
        body_lines.append(line)

    plain_body = "\n".join(body_lines).strip()

    esc = html.escape
    paragraphs_html = "".join(
        f"<p>{esc(p).replace('**', '')}</p>"
        for p in plain_body.split("\n\n")
        if p.strip()
    )
    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8" /></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #1e293b; line-height: 1.6; max-width: 640px; margin: 24px auto; padding: 20px;">
  <div style="border-bottom: 2px solid #0f172a; padding-bottom: 12px; margin-bottom: 18px;">
    <strong style="font-size: 18px; color: #0f172a;">{esc(settings.agency.agency_name)}</strong>
  </div>
  {paragraphs_html}
</body>
</html>
"""
    return subject, plain_body, html_body


def write_welcome_email_draft(
    client: ClientRecord,
    workspace_location: str,
    invoice: InvoiceResult,
    output_dir: Path,
    settings: PipelineSettings,
) -> tuple[Path, Path]:
    """Save personalized plaintext and HTML welcome email drafts in the client's workspace."""
    subject, plain_body, html_body = render_welcome_email(
        client, workspace_location, invoice, settings
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_path = output_dir / "welcome_email_draft.txt"
    txt_content = (
        f"To: {client.email or '[Add client email]'}\n"
        f"Subject: {subject}\n\n"
        f"{plain_body}\n"
    )
    txt_path.write_text(txt_content, encoding="utf-8")

    html_path = output_dir / "welcome_email_draft.html"
    html_path.write_text(html_body, encoding="utf-8")
    return txt_path, html_path


def _is_retryable_smtp_error(exc: Exception) -> bool:
    if isinstance(exc, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return False
    if isinstance(exc, smtplib.SMTPResponseException) and 500 <= exc.smtp_code < 600:
        return False
    return isinstance(exc, (smtplib.SMTPException, TimeoutError, OSError))


def send_email_smtp(
    *,
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str,
    settings: PipelineSettings,
    attachments: list[Path] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Send a multipart MIME welcome email via Gmail or generic SMTP with transient retries."""
    if not settings.smtp.is_configured:
        raise ValueError(
            "SMTP credentials missing. Configure GMAIL_ADDRESS/GMAIL_APP_PASSWORD or SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD."
        )
    if not to_email.strip():
        raise ValueError("Recipient email address is empty.")

    msg = EmailMessage()
    msg["From"] = settings.smtp.from_address or settings.smtp.username
    msg["To"] = to_email.strip()
    msg["Subject"] = subject
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    for attachment in attachments or []:
        if attachment.exists() and attachment.is_file():
            ctype, _ = mimetypes.guess_type(str(attachment))
            maintype, subtype = (ctype.split("/", 1) if ctype and "/" in ctype else ("application", "octet-stream"))
            msg.add_attachment(
                attachment.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=attachment.name,
            )

    def _deliver() -> None:
        if settings.smtp.use_ssl:
            with smtplib.SMTP_SSL(settings.smtp.host, settings.smtp.port, timeout=20) as smtp:
                smtp.login(settings.smtp.username, settings.smtp.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(settings.smtp.host, settings.smtp.port, timeout=20) as smtp:
                if settings.smtp.use_tls:
                    smtp.starttls()
                smtp.login(settings.smtp.username, settings.smtp.password)
                smtp.send_message(msg)

    _, attempts = execute_with_retry(_deliver, settings.retry, _is_retryable_smtp_error, sleep_fn=sleep_fn)
    return attempts


def detect_webhook_format(url: str, configured_fmt: str = "auto") -> str:
    """Resolve webhook payload dialect based on explicit format or target URL."""
    fmt = configured_fmt.strip().lower()
    if fmt in {"slack", "discord", "generic"}:
        return fmt
    lowered = url.lower()
    if "hooks.slack.com" in lowered:
        return "slack"
    if "discord.com/api/webhooks" in lowered or "discordapp.com/api/webhooks" in lowered:
        return "discord"
    return "generic"


def build_webhook_payload(
    *,
    client_name: str,
    project_name: str,
    invoice_number: str,
    total_display: str,
    workspace_location: str,
    fmt: str = "generic",
) -> dict[str, Any]:
    """Build platform-native webhook JSON payloads for Slack, Discord, or generic consumers."""
    summary_text = (
        f"New client onboarded: {client_name} | {project_name} | "
        f"Invoice {invoice_number} ({total_display}) | Workspace: {workspace_location}"
    )

    if fmt == "slack":
        return {
            "text": summary_text,
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"Client Onboarded: {client_name}"},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Project:*\n{project_name}"},
                        {"type": "mrkdwn", "text": f"*Invoice:*\n{invoice_number} ({total_display})"},
                        {"type": "mrkdwn", "text": f"*Workspace:*\n{workspace_location}"},
                    ],
                },
            ],
        }

    if fmt == "discord":
        return {
            "content": f"**New Client Onboarded: {client_name}**",
            "embeds": [
                {
                    "title": f"Client Onboarded — {client_name}",
                    "color": 2278750,
                    "fields": [
                        {"name": "Project", "value": project_name, "inline": True},
                        {"name": "Invoice", "value": f"{invoice_number} ({total_display})", "inline": True},
                        {"name": "Workspace", "value": workspace_location, "inline": False},
                    ],
                    "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
            ],
        }

    return {
        "event": "client.onboarded",
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "text": summary_text,
        "client_name": client_name,
        "project_name": project_name,
        "invoice_number": invoice_number,
        "total_display": total_display,
        "workspace_location": workspace_location,
    }


def _is_retryable_http_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


def send_webhook_alert(
    *,
    client_name: str,
    project_name: str,
    invoice_number: str,
    total_display: str,
    workspace_location: str,
    settings: PipelineSettings,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[int, str]:
    """Post a structured onboarding alert to Slack, Discord, or generic webhook with retries."""
    url = settings.webhook.url.strip()
    if not url:
        raise ValueError("Missing WEBHOOK_URL configuration.")

    fmt = detect_webhook_format(url, settings.webhook.fmt)
    payload = build_webhook_payload(
        client_name=client_name,
        project_name=project_name,
        invoice_number=invoice_number,
        total_display=total_display,
        workspace_location=workspace_location,
        fmt=fmt,
    )
    data = json.dumps(payload).encode("utf-8")

    def _post() -> int:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "ClientOnboardingBot/2.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return int(getattr(resp, "status", 200))

    status_code, attempts = execute_with_retry(_post, settings.retry, _is_retryable_http_error, sleep_fn=sleep_fn)
    return attempts, f"{fmt}:HTTP_{status_code}"
