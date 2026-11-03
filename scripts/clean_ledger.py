"""Archive the AI trade ledger so the scorecard starts fresh from the fix point.

The old ``data/trades.jsonl`` contains phantom "sell up" (short YES) orders that
were accepted by Kalshi's API but never filled (the bot lacked USDC margin). Those
inflate the AI win/loss tally. This script archives the old ledger and starts a
clean one, so ``ai_live_from_ledger`` counts only real post-fix trades.

Run: python scripts/clean_ledger.py [--dry-run]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

LEDGER = Path("data/trades.jsonl")
ARCHIVE_DIR = Path("data/archive")


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive the AI trade ledger.")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen")
    args = parser.parse_args()

    if not LEDGER.exists():
        print(f"{LEDGER} does not exist — nothing to archive.")
        return 0

    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = ARCHIVE_DIR / f"trades_{stamp}.jsonl"
    size = LEDGER.stat().st_size

    if args.dry_run:
        print(f"[dry-run] would move {LEDGER} ({size} bytes) -> {dest}")
        return 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(LEDGER), str(dest))
    # Recreate an empty ledger so the running engine keeps appending.
    LEDGER.write_text("", encoding="utf-8")
    print(f"Archived {size} bytes to {dest}")
    print(f"Created a fresh empty {LEDGER} — AI scorecard now starts clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
