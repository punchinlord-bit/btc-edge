"""CLI entry point: evaluate a live prediction log (forward test).

Usage:
    python scripts/import_predictions.py predictions.csv
    python scripts/import_predictions.py predictions.jsonl
"""

from __future__ import annotations

import argparse
import sys

from btc_edge.historical.forward_test import compute_metrics, load_any, report_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward-test a prediction log")
    parser.add_argument("path", help="CSV or JSONL of (timestamp, prob, outcome)")
    args = parser.parse_args(argv)

    preds = load_any(args.path)
    if not preds:
        print("No predictions found in", args.path)
        return 1
    report = compute_metrics(preds)
    print(report_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
