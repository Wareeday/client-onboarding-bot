"""Root CLI entrypoint for Multi-Channel Client Onboarding Bot.

Examples:
    # Preview onboarding plan safely without writing files or calling APIs:
    python3 main.py --dry-run

    # Run locally on sample_data/clients_sample.csv (zero API keys, zero pip installs):
    python3 main.py

    # Validate CRM input & integration configuration:
    python3 main.py validate --input sample_data/clients_sample.csv

    # Inspect persisted onboarding state & regenerate executive HTML dashboard:
    python3 main.py status
    python3 main.py report
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from client_onboarding_bot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
