"""Workspace Builder: idempotent local & Google Drive client workspace provisioning."""

from __future__ import annotations

import csv
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from client_onboarding_bot.config import PipelineSettings
from client_onboarding_bot.models import ClientRecord
from client_onboarding_bot.state_store import StateStore


def slugify(text: str) -> str:
    """Convert arbitrary client names into filesystem-safe folder names."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip()).strip("_")
    return slug[:60] or "Client"


def resolve_workspace_slug(
    client: ClientRecord,
    base_dir: Path,
    *,
    store: StateStore | None = None,
    existing_slug: str | None = None,
) -> str:
    """Resolve a deterministic, collision-free folder slug for a client."""
    if existing_slug:
        return existing_slug

    base_slug = slugify(client.client_name)
    candidate = base_slug
    counter = 2

    while True:
        db_taken = store.is_slug_taken_by_other(candidate, client.idempotency_key) if store else False
        disk_taken = False
        if (base_dir / candidate).exists():
            # Allow reusing an existing directory only if it already belongs to this client in SQLite.
            existing = store.get_client(client.idempotency_key) if store else None
            if not existing or existing.get("workspace_slug") != candidate:
                disk_taken = True
        if not db_taken and not disk_taken:
            return candidate
        candidate = f"{base_slug[:55]}_{counter}"
        counter += 1


def _write_project_tracker(
    tracker_path: Path,
    client: ClientRecord,
    settings: PipelineSettings,
    start_dt: date,
) -> None:
    with tracker_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Client",
                "Project",
                "Milestone",
                "Day Offset",
                "Target Date",
                "Owner",
                "Status",
                "Notes",
            ],
        )
        writer.writeheader()
        for milestone in settings.milestones:
            target = (start_dt + timedelta(days=milestone.day_offset)).strftime("%Y-%m-%d")
            writer.writerow(
                {
                    "Client": client.client_name,
                    "Project": client.project_name,
                    "Milestone": milestone.name,
                    "Day Offset": milestone.day_offset,
                    "Target Date": target,
                    "Owner": settings.agency.sender_name,
                    "Status": milestone.status,
                    "Notes": milestone.notes,
                }
            )


def _write_kickoff_brief(
    brief_path: Path,
    client: ClientRecord,
    settings: PipelineSettings,
    start_dt: date,
) -> None:
    folder_lines = "\n".join(f"- `{sub}/`" for sub in settings.subfolders)
    milestone_rows = "\n".join(
        f"| {m.name} | Day {m.day_offset} | {(start_dt + timedelta(days=m.day_offset)).strftime('%Y-%m-%d')} | {m.status} |"
        for m in settings.milestones
    )
    tier_line = f"- **Package / Tier:** {client.tier}\n" if client.tier else ""
    notes_section = f"\n## Engagement Notes\n\n{client.notes}\n" if client.notes else ""

    content = f"""# Client Kickoff Brief — {client.client_name}

- **Project:** {client.project_name}
- **Primary Contact:** {client.contact_name or 'N/A'} ({client.email or 'N/A'})
- **Onboarding Date:** {start_dt.strftime('%Y-%m-%d')}
- **Account Lead:** {settings.agency.sender_name} ({settings.agency.agency_name})
{tier_line}
## Workspace Structure

{folder_lines}

## Delivery Milestones

| Milestone | Schedule | Target Date | Status |
| :--- | :--- | :--- | :--- |
{milestone_rows}
{notes_section}"""
    brief_path.write_text(content, encoding="utf-8")


def create_local_workspace(
    client: ClientRecord,
    settings: PipelineSettings,
    *,
    slug: str | None = None,
    start_date: date | None = None,
) -> dict[str, Path]:
    """Create the client's local folder tree, Project_Tracker.csv, and Kickoff_Brief.md."""
    chosen_slug = slug or slugify(client.client_name)
    root = settings.clients_dir / chosen_slug
    root.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {"root": root}
    for folder_name in settings.subfolders:
        sub_path = root / folder_name
        sub_path.mkdir(parents=True, exist_ok=True)
        paths[folder_name] = sub_path

    start_dt = start_date or client.start_date or date.today()
    tracker_path = root / "Project_Tracker.csv"
    _write_project_tracker(tracker_path, client, settings, start_dt)
    paths["tracker"] = tracker_path

    brief_path = root / "Kickoff_Brief.md"
    _write_kickoff_brief(brief_path, client, settings, start_dt)
    paths["brief"] = brief_path

    return paths


def _execute_drive_call(fn: Callable[[], Any], settings: PipelineSettings) -> Any:
    """Retry transient Google Drive HTTP 429/5xx or socket errors with exponential backoff."""
    attempts = max(1, settings.retry.max_attempts)
    delay = settings.retry.base_delay_seconds
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - requires googleapiclient
            last_exc = exc
            status = getattr(getattr(exc, "resp", None), "status", None)
            retryable = isinstance(exc, OSError) or status in {429, 500, 502, 503, 504}
            if not retryable or attempt >= attempts:
                raise
            time.sleep(delay)
            delay *= settings.retry.backoff_multiplier
    assert last_exc is not None
    raise last_exc


def create_google_drive_workspace(
    client: ClientRecord,
    settings: PipelineSettings,
) -> str:
    """Idempotently create the client's folder hierarchy in Google Drive via Drive API v3."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive dependencies are not installed. "
            "Run `pip install google-api-python-client google-auth` (or `pip install -e '.[gdrive]'`)."
        ) from exc

    creds_path = Path(settings.drive.credentials_path)
    if not creds_path.is_absolute():
        creds_path = settings.base_dir / creds_path
    if not creds_path.exists():
        raise FileNotFoundError(f"Service account JSON not found at '{creds_path}'.")

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = service_account.Credentials.from_service_account_file(str(creds_path), scopes=scopes)
    service = build("drive", "v3", credentials=creds)

    parent_id = settings.drive.parent_folder_id.strip()
    root_folder_name = f"Client - {client.client_name}"

    def _find_or_create_folder(name: str, parent: str) -> dict[str, Any]:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query_parts = [
            f"name = '{escaped}'",
            "mimeType = 'application/vnd.google-apps.folder'",
            "trashed = false",
        ]
        if parent:
            query_parts.append(f"'{parent}' in parents")
        query = " and ".join(query_parts)

        res = _execute_drive_call(
            lambda: service.files()
            .list(q=query, fields="files(id, name, webViewLink)", pageSize=1)
            .execute(),
            settings,
        )
        files = res.get("files", [])
        if files:
            return dict(files[0])

        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent:
            metadata["parents"] = [parent]
        return dict(
            _execute_drive_call(
                lambda: service.files().create(body=metadata, fields="id, webViewLink").execute(),
                settings,
            )
        )

    root_folder = _find_or_create_folder(root_folder_name, parent_id)
    folder_id = root_folder["id"]

    for sub in settings.subfolders:
        _find_or_create_folder(sub, folder_id)

    return str(root_folder.get("webViewLink", f"https://drive.google.com/drive/folders/{folder_id}"))
