"""CLI entry point for the historical bootstrap.

Usage:
    python scripts/run_bootstrap.py --synthetic-if-offline --windows 600
"""

from __future__ import annotations

import sys

from btc_edge.historical.bootstrapper import cli

if __name__ == "__main__":
    sys.exit(cli())
