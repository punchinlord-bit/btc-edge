"""Launch a second BTC-Edge instance for the Partner (User 2).

Runs the engine with a separate ``.env.partner`` file (separate Kalshi/Polymarket
keys), a separate database, and a separate dashboard port — so two people run
side by side without sharing money or state.

Usage:
    python scripts/run_partner.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PARTNER_PORT = "8081"
PARTNER_ENV = Path(".env.partner")
PARTNER_ENV_EXAMPLE = Path(".env.partner.example")


def _ensure_partner_env() -> None:
    """Create .env.partner from the committed example template if it is missing."""
    if not PARTNER_ENV.exists() and PARTNER_ENV_EXAMPLE.exists():
        shutil.copyfile(PARTNER_ENV_EXAMPLE, PARTNER_ENV)


def main() -> int:
    _ensure_partner_env()
    env = os.environ.copy()
    env["BTC_EDGE_ENV_FILE"] = str(PARTNER_ENV)
    env["BTC_EDGE_DB_PATH"] = "data/btc_edge_partner.db"
    env["PORT"] = PARTNER_PORT
    print(f"Starting partner instance on http://localhost:{PARTNER_PORT}")
    return subprocess.run([sys.executable, "scripts/run_engine.py"], env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
