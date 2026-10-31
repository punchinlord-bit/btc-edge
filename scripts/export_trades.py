"""Export and analyze your personal Kalshi trade history.

Pulls filled orders (``/portfolio/fills``) and settled positions
(``/portfolio/settlements``) and writes them to ``data/trades/`` so the model
can be validated against *your* real outcomes — the only honest way to know if
the bot has an edge. Also prints a plain-language summary.

Requires ``KALSHI_ACCESS_KEY_ID`` + ``KALSHI_PRIVATE_KEY`` in the environment
(or ``.env``).

Usage:
    python scripts/export_trades.py --ticker KXBTC15M --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from btc_edge.config import load_settings
from btc_edge.connectors.kalshi_client import KalshiClient, build_kalshi_signer_from_env

OUT_DIR = Path("data/trades")


async def _run(ticker: str, limit: int) -> int:
    settings = load_settings()
    signer = build_kalshi_signer_from_env()
    if signer is None:
        print(
            "No Kalshi credentials found. Set KALSHI_ACCESS_KEY_ID and "
            "KALSHI_PRIVATE_KEY (or KALSHI_API_KEY_ID) in your .env, then re-run.",
            file=sys.stderr,
        )
        return 2

    client = KalshiClient(signer=signer, base_url=settings.kalshi.active_base_url)

    # Paginate fills via cursor.
    fills: list[dict] = []
    cursor: str | None = None
    while True:
        page = await client.get_fills(ticker=ticker or None, limit=min(limit, 500), cursor=cursor)
        batch = page.get("fills", [])
        fills.extend(batch)
        cursor = page.get("cursor")
        if not cursor or not batch or len(fills) >= limit:
            break

    # Settled positions (realized PnL).
    try:
        settlements = await client.get_settlements(ticker=ticker or None, limit=min(limit, 500))
        settled = settlements.get("settlements", [])
    except Exception as exc:  # noqa: BLE001
        print(f"Could not fetch settlements: {exc}")
        settled = []

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "kalshi_fills.jsonl").write_text(
        "\n".join(json.dumps(f) for f in fills) + ("\n" if fills else ""), encoding="utf-8"
    )
    if settled:
        (OUT_DIR / "kalshi_settlements.jsonl").write_text(
            "\n".join(json.dumps(s) for s in settled) + "\n", encoding="utf-8"
        )

    print(f"Fills exported: {len(fills)}  -> {OUT_DIR / 'kalshi_fills.jsonl'}")
    print(f"Settlements exported: {len(settled)}  -> {OUT_DIR / 'kalshi_settlements.jsonl'}")
    print()
    print("===== YOUR TRADE HISTORY SUMMARY =====")
    buys = sum(1 for f in fills if f.get("action") == "buy")
    sells = len(fills) - buys
    total_cost = sum(
        (float(f.get("yes_price") or 0) / 100.0) * float(f.get("count") or 0)
        for f in fills
    )
    total_fees = sum(float(f.get("total_fee") or 0) / 100.0 for f in fills)
    print(f"Total filled orders: {len(fills)}  (buys: {buys}, sells: {sells})")
    print(f"Total notional (yes contracts): ${total_cost:,.2f}")
    print(f"Total fees paid: ${total_fees:,.2f}")

    realized = sum(float(s.get("realized_pnl") or 0) / 100.0 for s in settled)
    wins = sum(1 for s in settled if (float(s.get("realized_pnl") or 0)) > 0)
    if settled:
        print(f"Settled positions: {len(settled)}  (wins: {wins})")
        print(f"Realized P&L: ${realized:,.2f}")
        print(f"Win rate: {wins / len(settled):.1%}")
    else:
        print("(No settled positions returned — no realized P&L to report yet.)")
    print()
    print(
        "This is the record you should validate the model against. A profitable "
        "bot must show positive realized P&L *net of fees* over a meaningful "
        "number of trades — not just a high win rate."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Kalshi trade history")
    parser.add_argument("--ticker", default="KXBTC15M")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    return asyncio.run(_run(args.ticker, args.limit))


if __name__ == "__main__":
    sys.exit(main())
