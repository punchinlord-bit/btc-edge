"""Place a $0.01 (1-cent) test order to verify live order wiring.

For Kalshi this buys 1 YES contract at $0.01 on the nearest BTC above-strike
market. For Polymarket it places a 1-share limit order at $0.01 on the nearest
up token. Safe, minimal, and confirms the async placement path actually works.

Usage:
    python scripts/test_live_order.py --venue kalshi
    python scripts/test_live_order.py --venue polymarket
    python scripts/test_live_order.py --venue both
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx
from dotenv import load_dotenv

from btc_edge.config import load_settings


async def _btc_price() -> float:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
        return float(r.json()["data"]["amount"])


async def test_kalshi(settings) -> bool:
    from btc_edge.connectors.kalshi_client import (
        KalshiClient,
        build_kalshi_signer_from_env,
        resolve_btc_15m_market,
    )

    signer = build_kalshi_signer_from_env()
    if signer is None:
        print("Kalshi: no API keys set — skipping (set KALSHI_ACCESS_KEY_ID + KALSHI_PRIVATE_KEY)")
        return False
    client = KalshiClient(signer, settings.kalshi.active_base_url)
    markets = await client.get_btc_markets()
    market = resolve_btc_15m_market(markets)
    if market is None:
        print("Kalshi: no active KXBTC15M window found")
        return False
    ticker = market["ticker"]
    print(f"Kalshi test: BUY 1 YES @ $0.01 on {ticker}")
    resp = await client.place_order(ticker=ticker, side="yes", action="buy", count=1, price=0.01)
    print("Kalshi response:", resp)
    return True


async def test_polymarket(settings) -> bool:
    import os

    from btc_edge.connectors.poly_client import GammaClient, PolymarketClient, compute_window_ts, slug_for

    wallet = os.getenv("POLYMARKET_WALLET") or settings.polymarket_wallet
    key = os.getenv("POLYMARKET_PRIVATE_KEY") or settings.polymarket_private_key
    if not wallet or not key:
        print("Polymarket: no wallet/private key set — skipping "
              "(set POLYMARKET_WALLET + POLYMARKET_PRIVATE_KEY)")
        return False
    gamma = GammaClient(settings.polymarket.gamma_api_url)
    client = PolymarketClient(gamma=gamma, wallet=wallet, private_key=key)
    import time

    window_ts = compute_window_ts(time.time(), 300)
    slug = slug_for(300, window_ts)
    res = gamma.resolve_market(slug, 300, window_ts)
    if res is None or not res.up_token_id:
        print("Polymarket: could not resolve 5m up token")
        return False
    secure = await client.secure_client()
    print(f"Polymarket test: BUY 1 share @ $0.01 on token {res.up_token_id[:12]}…")
    await secure.create_limit_order(token_id=res.up_token_id, price=0.01, size=1.0, side="BUY")
    print("Polymarket: limit order submitted")
    await client.close()
    return True


async def main() -> int:
    load_dotenv()  # load .env into os.environ so os.getenv() reads it
    parser = argparse.ArgumentParser(description="$0.01 live order test")
    parser.add_argument("--venue", default="both", choices=["kalshi", "polymarket", "both"])
    args = parser.parse_args()

    settings = load_settings()
    print(f"Mode={settings.trading.mode!r} live_allowed={settings.live_trading_allowed()}")
    ok = True
    if args.venue in ("kalshi", "both"):
        ok = await test_kalshi(settings) and ok
    if args.venue in ("polymarket", "both"):
        ok = await test_polymarket(settings) and ok
    print("TEST DONE" if ok else "TEST INCOMPLETE (missing keys)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
