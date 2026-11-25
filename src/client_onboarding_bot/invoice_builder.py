"""Financial-grade Invoice Builder using Decimal precision and XSS-safe print-ready HTML5."""

from __future__ import annotations

import html
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from client_onboarding_bot.config import PipelineSettings
from client_onboarding_bot.models import ClientRecord, InvoiceResult

_TWO_PLACES = Decimal("0.01")


def _q(val: Decimal) -> Decimal:
    return val.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def compute_invoice_amounts(
    client: ClientRecord,
    settings: PipelineSettings,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, str, str]:
    """Compute exact subtotal, discount, tax rate, tax amount, total, currency code & symbol."""
    subtotal = _q(client.project_fee)
    if client.discount_is_percent:
        discount_amount = _q(subtotal * client.discount)
    else:
        discount_amount = _q(min(client.discount, subtotal))

    taxable_base = max(Decimal("0.00"), subtotal - discount_amount)
    tax_rate = client.tax_rate if client.tax_rate is not None else settings.agency.tax_rate
    tax_amount = _q(taxable_base * tax_rate)
    total = _q(taxable_base + tax_amount)

    currency_code = client.currency_code or settings.agency.currency_code
    currency_symbol = client.currency_symbol or settings.agency.currency_symbol
    return subtotal, discount_amount, tax_rate, tax_amount, total, currency_code, currency_symbol


def generate_invoice(
    client: ClientRecord,
    invoices_dir: Path,
    invoice_number: str,
    settings: PipelineSettings,
    *,
    issue_date: date | None = None,
    write_files: bool = True,
) -> InvoiceResult:
    """Generate Markdown (.md) and print-ready HTML5 (.html) invoice documents."""
    today = issue_date or date.today()
    terms_days = (
        client.payment_terms_days
        if client.payment_terms_days is not None
        else settings.agency.invoice_due_days
    )
    due_date = today + timedelta(days=terms_days)

    subtotal, discount_amount, tax_rate, tax_amount, total, curr_code, curr_sym = compute_invoice_amounts(
        client, settings
    )
    tax_label = settings.agency.tax_label
    tax_pct_display = f"{(tax_rate * Decimal('100')).normalize()}%"

    discount_md_line = (
        f"- **Discount:** -{curr_sym}{discount_amount:,.2f}\n" if discount_amount > Decimal("0.00") else ""
    )
    tax_id_md = f" | Tax ID: {settings.agency.tax_id}" if settings.agency.tax_id else ""

    md_content = f"""# INVOICE {invoice_number}

- **From:** {settings.agency.agency_name} ({settings.agency.sender_email}){tax_id_md}
- **Bill To:** {client.client_name} ({client.contact_name or 'Accounts'}) — {client.email or 'N/A'}
- **Issue Date:** {today.strftime('%Y-%m-%d')}
- **Due Date:** {due_date.strftime('%Y-%m-%d')} (Net-{terms_days})
- **Currency:** {curr_code}

| Description | Qty | Rate | Line Total |
| :--- | :---: | ---: | ---: |
| {client.project_name} | 1 | {curr_sym}{subtotal:,.2f} | {curr_sym}{subtotal:,.2f} |

- **Subtotal:** {curr_sym}{subtotal:,.2f}
{discount_md_line}- **{tax_label} ({tax_pct_display}):** {curr_sym}{tax_amount:,.2f}
- **Total Due:** **{curr_sym}{total:,.2f} {curr_code}**

**Payment Instructions:** {settings.agency.payment_instructions}
"""

    esc = html.escape
    agency_meta_parts = [esc(settings.agency.sender_email)]
    if settings.agency.agency_address:
        agency_meta_parts.append(esc(settings.agency.agency_address))
    if settings.agency.tax_id:
        agency_meta_parts.append(f"Tax ID: {esc(settings.agency.tax_id)}")
    if settings.agency.website:
        agency_meta_parts.append(esc(settings.agency.website))
    agency_meta_html = "<br/>".join(p for p in agency_meta_parts if p)

    discount_html_row = (
        f'<div class="sum-row"><span>Discount</span><span>-{esc(curr_sym)}{discount_amount:,.2f}</span></div>'
        if discount_amount > Decimal("0.00")
        else ""
    )
    client_id_html = f"<br/>Client ID: {esc(client.client_id)}" if client.client_id else ""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Invoice {esc(invoice_number)}</title>
<style>
  @page {{ size: A4; margin: 18mm; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    color: #0f172a;
    background: #f8fafc;
    margin: 0;
    padding: 32px 16px;
    line-height: 1.5;
  }}
  .sheet {{
    max-width: 780px;
    margin: 0 auto;
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 40px;
    box-shadow: 0 4px 20px rgba(15, 23, 42, 0.05);
  }}
  .header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 24px;
    border-bottom: 2px solid #0f172a;
    padding-bottom: 20px;
    margin-bottom: 28px;
  }}
  .brand h1 {{ margin: 0 0 6px 0; font-size: 24px; letter-spacing: -0.02em; }}
  .brand .sub {{ color: #475569; font-size: 13px; }}
  .inv-meta {{ text-align: right; }}
  .badge {{
    display: inline-block;
    background: #dcfce7;
    color: #166534;
    font-weight: 700;
    font-size: 11px;
    letter-spacing: 0.06em;
    padding: 3px 10px;
    border-radius: 999px;
    margin-bottom: 8px;
  }}
  .inv-number {{ font-size: 20px; font-weight: 700; color: #0f172a; }}
  .inv-dates {{ color: #475569; font-size: 13px; margin-top: 4px; }}
  .parties {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
    margin-bottom: 28px;
  }}
  .card {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 14px 16px;
    font-size: 14px;
  }}
  .card .label {{
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #64748b;
    margin-bottom: 6px;
  }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0 24px 0; }}
  th {{
    background: #0f172a;
    color: #ffffff;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 12px;
    text-align: left;
  }}
  td {{
    border-bottom: 1px solid #e2e8f0;
    padding: 14px 12px;
    font-size: 14px;
  }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .summary {{
    margin-left: auto;
    width: 300px;
    border-top: 1px solid #e2e8f0;
    padding-top: 10px;
  }}
  .sum-row {{
    display: flex;
    justify-content: space-between;
    padding: 5px 0;
    font-size: 14px;
    color: #334155;
  }}
  .sum-total {{
    display: flex;
    justify-content: space-between;
    margin-top: 10px;
    padding-top: 10px;
    border-top: 2px solid #0f172a;
    font-size: 18px;
    font-weight: 700;
    color: #0f172a;
  }}
  .notes {{
    margin-top: 32px;
    padding: 16px;
    background: #f1f5f9;
    border-left: 4px solid #0f172a;
    border-radius: 4px;
    font-size: 13px;
    color: #334155;
  }}
  @media print {{
    body {{ background: #ffffff; padding: 0; }}
    .sheet {{ border: none; box-shadow: none; padding: 0; max-width: 100%; }}
  }}
</style>
</head>
<body>
  <div class="sheet">
    <div class="header">
      <div class="brand">
        <h1>{esc(settings.agency.agency_name)}</h1>
        <div class="sub">{agency_meta_html}</div>
      </div>
      <div class="inv-meta">
        <div class="badge">ISSUED</div>
        <div class="inv-number">{esc(invoice_number)}</div>
        <div class="inv-dates">
          Issue Date: {today.strftime('%Y-%m-%d')}<br/>
          Due Date: {due_date.strftime('%Y-%m-%d')} (Net-{terms_days})
        </div>
      </div>
    </div>

    <div class="parties">
      <div class="card">
        <div class="label">Bill To</div>
        <strong>{esc(client.client_name)}</strong><br/>
        Attn: {esc(client.contact_name or 'Accounts Payable')}<br/>
        {esc(client.email or 'N/A')}{client_id_html}
      </div>
      <div class="card">
        <div class="label">Engagement Summary</div>
        <strong>{esc(client.project_name)}</strong><br/>
        Currency: {esc(curr_code)}<br/>
        Prepared By: {esc(settings.agency.sender_name)}
      </div>
    </div>

    <table>
      <thead>
        <tr>
          <th>Description</th>
          <th class="num">Qty</th>
          <th class="num">Unit Rate</th>
          <th class="num">Line Total</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>{esc(client.project_name)}</td>
          <td class="num">1</td>
          <td class="num">{esc(curr_sym)}{subtotal:,.2f}</td>
          <td class="num">{esc(curr_sym)}{subtotal:,.2f}</td>
        </tr>
      </tbody>
    </table>

    <div class="summary">
      <div class="sum-row"><span>Subtotal</span><span>{esc(curr_sym)}{subtotal:,.2f}</span></div>
      {discount_html_row}
      <div class="sum-row"><span>{esc(tax_label)} ({esc(tax_pct_display)})</span><span>{esc(curr_sym)}{tax_amount:,.2f}</span></div>
      <div class="sum-total"><span>Total Due</span><span>{esc(curr_sym)}{total:,.2f} {esc(curr_code)}</span></div>
    </div>

    <div class="notes">
      <strong>Payment Instructions:</strong><br/>
      {esc(settings.agency.payment_instructions)}
    </div>
  </div>
</body>
</html>
"""

    md_path: Path | None = None
    html_path: Path | None = None
    if write_files:
        invoices_dir.mkdir(parents=True, exist_ok=True)
        md_path = invoices_dir / f"{invoice_number}.md"
        html_path = invoices_dir / f"{invoice_number}.html"
        md_path.write_text(md_content, encoding="utf-8")
        html_path.write_text(html_content, encoding="utf-8")

    return InvoiceResult(
        invoice_number=invoice_number,
        issue_date=today,
        due_date=due_date,
        currency_code=curr_code,
        currency_symbol=curr_sym,
        subtotal=subtotal,
        discount_amount=discount_amount,
        tax_label=tax_label,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        md_path=md_path,
        html_path=html_path,
    )
