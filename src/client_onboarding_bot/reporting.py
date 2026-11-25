"""Execution telemetry and HTML5 executive onboarding dashboard generator."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from client_onboarding_bot.config import PipelineSettings
from client_onboarding_bot.models import RunSummary
from client_onboarding_bot.state_store import StateStore


def _compute_ledger_metrics(clients: list[dict[str, Any]]) -> dict[str, Any]:
    revenue_by_currency: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    status_counts: dict[str, int] = defaultdict(int)
    failed_channels = 0

    for c in clients:
        status = str(c.get("status", "completed"))
        status_counts[status] += 1
        curr = str(c.get("currency_code") or "USD")
        try:
            revenue_by_currency[curr] += Decimal(str(c.get("total_amount") or "0"))
        except Exception:
            pass
        for ch in c.get("channels", []):
            if ch.get("status") == "failed":
                failed_channels += 1

    return {
        "total_clients": len(clients),
        "status_counts": dict(status_counts),
        "revenue_by_currency": {k: f"{v:,.2f}" for k, v in sorted(revenue_by_currency.items())},
        "failed_channels": failed_channels,
    }


def render_dashboard_html(
    *,
    clients: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    settings: PipelineSettings,
    latest_summary: RunSummary | None = None,
) -> str:
    """Render an offline, self-contained HTML5 executive onboarding & revenue dashboard."""
    esc = html.escape
    metrics = _compute_ledger_metrics(clients)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    revenue_badges = " &nbsp;|&nbsp; ".join(
        f"{esc(curr)} {esc(val)}" for curr, val in metrics["revenue_by_currency"].items()
    ) or "0.00"

    client_rows_html = []
    for c in clients:
        st = str(c.get("status", "completed"))
        badge_cls = {
            "completed": "badge-ok",
            "partial": "badge-warn",
            "failed": "badge-err",
        }.get(st, "badge-ok")

        channel_pills = []
        for ch in c.get("channels", []):
            ch_st = ch.get("status", "")
            ch_cls = {
                "success": "pill-ok",
                "failed": "pill-err",
                "skipped": "pill-muted",
            }.get(ch_st, "pill-muted")
            channel_pills.append(
                f'<span class="pill {ch_cls}" title="{esc(ch.get("last_error", ""))}">'
                f'{esc(ch.get("channel", ""))}: {esc(ch_st)}</span>'
            )

        client_rows_html.append(
            "<tr>"
            f"<td><strong>{esc(str(c.get('client_name', '')))}</strong><br/>"
            f"<span class='sub'>{esc(str(c.get('email', '')))}</span></td>"
            f"<td>{esc(str(c.get('project_name', '')))}</td>"
            f"<td><code>{esc(str(c.get('invoice_number', '')))}</code></td>"
            f"<td class='num'>{esc(str(c.get('currency_code', 'USD')))} {esc(str(c.get('total_amount', '0.00')))}</td>"
            f"<td>{esc(str(c.get('onboarded_date', '')))}</td>"
            f"<td><span class='badge {badge_cls}'>{esc(st.upper())}</span></td>"
            f"<td>{' '.join(channel_pills) or '<span class=\"sub\">—</span>'}</td>"
            "</tr>"
        )

    rows_markup = "\n".join(client_rows_html) or (
        '<tr><td colspan="7" class="empty">No onboarded clients recorded yet.</td></tr>'
    )

    run_banner = ""
    if latest_summary is not None:
        run_banner = (
            f"<div class='banner'><strong>Latest Run ({esc(latest_summary.run_id)}):</strong> "
            f"Onboarded {latest_summary.onboarded_count}, Partial {latest_summary.partial_count}, "
            f"Skipped {latest_summary.skipped_count}, Failed {latest_summary.failed_count} "
            f"({esc(latest_summary.mode)} mode)</div>"
        )
    elif runs:
        r0 = runs[0]
        run_banner = (
            f"<div class='banner'><strong>Last Recorded Run ({esc(str(r0.get('run_id', '')))}):</strong> "
            f"Onboarded {r0.get('onboarded_count', 0)}, Partial {r0.get('partial_count', 0)}, "
            f"Skipped {r0.get('skipped_count', 0)}, Failed {r0.get('failed_count', 0)}</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{esc(settings.agency.agency_name)} — Onboarding Dashboard</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    margin: 0;
    padding: 32px 20px;
    background: #f1f5f9;
    color: #0f172a;
  }}
  .wrap {{ max-width: 1120px; margin: 0 auto; }}
  .top {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
  h1 {{ margin: 0; font-size: 24px; }}
  .meta {{ color: #475569; font-size: 13px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .kpi {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 18px 20px;
  }}
  .kpi .label {{ font-size: 12px; font-weight: 600; text-transform: uppercase; color: #64748b; letter-spacing: 0.05em; }}
  .kpi .val {{ font-size: 24px; font-weight: 700; margin-top: 6px; color: #0f172a; }}
  .banner {{
    background: #e0f2fe;
    border: 1px solid #bae6fd;
    color: #0c4a6e;
    padding: 12px 16px;
    border-radius: 8px;
    margin-bottom: 20px;
    font-size: 14px;
  }}
  .panel {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    overflow-x: auto;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{
    text-align: left;
    padding: 12px 14px;
    background: #0f172a;
    color: #ffffff;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  td {{ padding: 12px 14px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
  .sub {{ color: #64748b; font-size: 12px; }}
  .badge {{
    display: inline-block; padding: 3px 9px; border-radius: 999px;
    font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
  }}
  .badge-ok {{ background: #dcfce7; color: #166534; }}
  .badge-warn {{ background: #fef3c7; color: #92400e; }}
  .badge-err {{ background: #fee2e2; color: #991b1b; }}
  .pill {{
    display: inline-block; margin: 2px 4px 2px 0; padding: 2px 7px; border-radius: 4px;
    font-size: 11px; font-weight: 600;
  }}
  .pill-ok {{ background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }}
  .pill-err {{ background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }}
  .pill-muted {{ background: #f8fafc; color: #64748b; border: 1px solid #e2e8f0; }}
  .empty {{ text-align: center; padding: 32px; color: #64748b; }}
  code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 12px; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>{esc(settings.agency.agency_name)} — Client Onboarding Ledger</h1>
      <div class="meta">Generated: {esc(generated_at)}</div>
    </div>
    <div class="kpis">
      <div class="kpi"><div class="label">Total Onboarded Clients</div><div class="val">{metrics['total_clients']}</div></div>
      <div class="kpi"><div class="label">Total Invoiced Value</div><div class="val">{revenue_badges}</div></div>
      <div class="kpi"><div class="label">Completed / Partial</div><div class="val">{metrics['status_counts'].get('completed', 0)} / {metrics['status_counts'].get('partial', 0)}</div></div>
      <div class="kpi"><div class="label">Pending Delivery Retries</div><div class="val">{metrics['failed_channels']}</div></div>
    </div>
    {run_banner}
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>Client</th>
            <th>Project</th>
            <th>Invoice</th>
            <th class="num">Total</th>
            <th>Onboarded</th>
            <th>Status</th>
            <th>Delivery Channels</th>
          </tr>
        </thead>
        <tbody>
          {rows_markup}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""


def write_run_report(
    *,
    summary: RunSummary,
    store: StateStore,
    settings: PipelineSettings,
) -> tuple[Path, Path]:
    """Write structured JSON run telemetry and refresh the HTML onboarding dashboard."""
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = settings.reports_dir / "latest_run.json"
    json_path.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")

    html_path = generate_dashboard_from_store(store=store, settings=settings, latest_summary=summary)
    return json_path, html_path


def generate_dashboard_from_store(
    *,
    store: StateStore,
    settings: PipelineSettings,
    latest_summary: RunSummary | None = None,
) -> Path:
    """Render and save the HTML onboarding dashboard from persisted SQLite state."""
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    clients = store.list_clients(limit=500)
    runs = store.list_runs(limit=20)
    html_content = render_dashboard_html(
        clients=clients,
        runs=runs,
        settings=settings,
        latest_summary=latest_summary,
    )
    html_path = settings.reports_dir / "onboarding_dashboard.html"
    html_path.write_text(html_content, encoding="utf-8")
    return html_path
