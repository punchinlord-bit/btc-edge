"""CLI entry point for the fee-aware backtest.

Usage:
    python scripts/run_backtest.py --historical-dir data/historical
"""

from __future__ import annotations

import sys

from btc_edge.backtest.report import cli

if __name__ == "__main__":
    sys.exit(cli())
