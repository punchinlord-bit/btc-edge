"""Fully automated BTC-Edge setup and run.

One command does everything:

    python scripts/run_all.py

Steps:
1.  Check for Kalshi API keys in ``.env``. If missing, open your browser to the
    Kalshi API-key page and prompt you to paste the two values (the one step that
    requires you — only you can generate keys on Kalshi's site).
2.  Export your personal trade history (``/portfolio/fills`` +
    ``/portfolio/settlements``) to ``data/trades/`` and print a summary.
3.  Start the trading engine + dashboard.

Flags: ``--skip-login``, ``--skip-export``, ``--skip-engine``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

ENV_PATH = Path(".env")
KALSHI_API_KEYS_URL = "https://kalshi.com/account/api-keys"


def _read_env() -> str:
    return ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""


def _env_value(content: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.*)$", content, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def check_kalshi_credentials() -> bool:
    """Return True if Kalshi API key id + private key are present in .env."""
    content = _read_env()
    return bool(_env_value(content, "KALSHI_API_KEY_ID") and _env_value(content, "KALSHI_PRIVATE_KEY"))


def save_credentials(key_id: str, private_key: str) -> None:
    """Persist the two Kalshi keys into .env (creating the file if needed)."""
    content = _read_env()
    if not content:
        content = (
            "# BTC-Edge environment variables (auto-generated).\n"
            "ALLOW_LIVE_TRADING=FALSE\n"
            "DASHBOARD_API_KEY=btcmoney\n"
            "KALSHI_USE_DEMO=true\n"
        )
    for key, value in (("KALSHI_API_KEY_ID", key_id), ("KALSHI_PRIVATE_KEY", private_key)):
        if re.search(rf"^{re.escape(key)}=", content, flags=re.MULTILINE):
            content = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
        else:
            content += f"{key}={value}\n"
    ENV_PATH.write_text(content, encoding="utf-8")


def run_browser_login() -> bool:
    """Open the Kalshi API-key page and prompt for the two values."""
    print("\n" + "=" * 60)
    print("STEP 1: Kalshi API keys")
    print("=" * 60)
    print("\nOpening Kalshi API-key page in your browser...")
    print("1. Log in (if prompted)")
    print("2. Create a new API key (copy the Key ID and Private Key)")
    print("   (Kalshi shows the Private Key only once — copy it immediately.)\n")
    webbrowser.open(KALSHI_API_KEYS_URL)
    input("Press Enter when you have both values copied...\n")

    key_id = input("Paste Kalshi API Key ID: ").strip()
    private_key = input("Paste Kalshi Private Key: ").strip()
    if not key_id or not private_key:
        print("\nNo credentials entered — skipping. Re-run when ready.")
        return False

    save_credentials(key_id, private_key)
    print("\nCredentials saved to .env")
    return True


def run_export_trades() -> bool:
    """Export trade history and print the summary."""
    print("\n" + "=" * 60)
    print("STEP 2: Export trade history")
    print("=" * 60)
    try:
        result = subprocess.run(
            [sys.executable, "scripts/export_trades.py", "--ticker", "KXBTC15M", "--limit", "500"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        print(result.stdout)
        if result.stderr:
            print("stderr:", result.stderr)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("Export timed out.")
        return False


def run_engine() -> None:
    """Start the trading engine (blocks until Ctrl+C)."""
    print("\n" + "=" * 60)
    print("STEP 3: Start trading engine")
    print("=" * 60)
    print("\nDashboard: http://localhost:8080  (key: btcmoney)")
    print("Press Ctrl+C to stop.\n")
    subprocess.run([sys.executable, "scripts/run_engine.py"], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fully automated BTC-Edge setup and run")
    parser.add_argument("--skip-login", action="store_true", help="skip Kalshi login step")
    parser.add_argument("--skip-export", action="store_true", help="skip trade-history export")
    parser.add_argument("--skip-engine", action="store_true", help="skip starting the engine")
    args = parser.parse_args()

    if not args.skip_login and not check_kalshi_credentials():
        print("Kalshi credentials not found in .env.")
        if not run_browser_login():
            return 1
    elif not args.skip_login:
        print("Kalshi credentials found in .env.")

    if not args.skip_export and not run_export_trades():
        print("Trade-history export had issues — continuing anyway.")

    if not args.skip_engine:
        run_engine()

    return 0


if __name__ == "__main__":
    sys.exit(main())
