"""Central configuration defaults for the Multi-Channel Client Onboarding Bot.

Customize your agency details, folder structure, invoice terms, milestones,
and welcome email template here, or override declaratively via `config.yaml`
and environment variables (`.env`).
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_DATA_DIR = BASE_DIR / "sample_data"
OUTPUT_DIR = BASE_DIR / "output"
CLIENTS_OUTPUT_DIR = OUTPUT_DIR / "clients"

# Agency / Billing Profile (rendered on generated invoices, kickoff briefs & welcome emails)
AGENCY_INFO = {
    "agency_name": "Your Automation Studio",
    "sender_name": "Your Name",
    "sender_email": "you@example.com",
    "agency_address": "100 Innovation Way, Suite 400",
    "tax_id": "",
    "website": "https://example.com",
    "payment_instructions": "Bank transfer / Wise — settlement details provided upon kickoff.",
    "currency_symbol": "$",
    "currency_code": "USD",
    "tax_label": "Tax",
    "tax_rate": 0.00,          # Decimal fraction, e.g. 0.00 (0%), 0.08 (8%), 0.15 (15%)
    "invoice_due_days": 14,    # Net-14 payment term
    "invoice_prefix": "INV",
}

# Standard subfolders provisioned for every newly won client
CLIENT_SUBFOLDERS = [
    "01_Contracts_and_Invoices",
    "02_Client_Assets",
    "03_Automation_Deliverables",
    "04_Weekly_Reports",
]

# Default project kickoff milestones: (title, day_offset, default_status)
DEFAULT_MILESTONES = [
    ("Kickoff Call & Access Setup", 1, "Not Started"),
    ("Architecture & Prototype Demo", 5, "Not Started"),
    ("Testing on Sample Data", 9, "Not Started"),
    ("Final Handover & Documentation", 14, "Not Started"),
]

# Welcome email template (rendered to both plaintext draft and multipart HTML)
WELCOME_EMAIL_TEMPLATE = """Subject: Welcome to {agency_name}, {client_name}! (Kickoff & Next Steps)

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
