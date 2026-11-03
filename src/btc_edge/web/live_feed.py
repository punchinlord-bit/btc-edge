"""Live market feed: BTC price, Polymarket + Kalshi markets, window countdowns.

Polls public market-data endpoints (no API keys required for reads) on a short
cadence and mirrors the results into the shared ``EngineState`` so the dashboard
shows live, accurate numbers — a moving BTC price, per-venue probability meters,
and 5m/15m window countdown progress bars.

All fetches are best-effort: a transient network error is logged and skipped, and
never crashes the engine loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from btc_edge.connectors.poly_client import next_window_ts
from btc_edge.web.app import EngineState

logger = logging.getLogger(__name__)

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
TIMEFRAMES: tuple[tuple[int, str], ...] = ((300, "5m"), (900, "15m"), (86400, "1d"))
MAX_HISTORY_POINTS = 240


def compute_countdowns(now: float | None = None) -> dict[str, float]:
    """Seconds remaining in the current 5m/15m windows (wall-clock accurate)."""
    now = now if now is not None else time.time()
    return {label: max(0.0, next_window_ts(now, tf) - now) for tf, label in TIMEFRAMES}


def to_probability(value: Any) -> float | None:
    """Normalize a market price to a [0, 1] probability.

    Accepts either a 0-1 fraction or a 0-100 cents/integer representation (as
    returned by Kalshi).
    """
    if value is None or value == "":
        return None
    f = float(value)
    if 0.0 <= f <= 1.0:
        return f
    if 1.0 < f <= 100.0:
        return f / 100.0
    return None


class LiveMarketFeed:
    """Fetches live public market data and mirrors it into ``EngineState``."""

    def __init__(
        self,
        state: EngineState,
        *,
        coinbase_url: str = COINBASE_SPOT_URL,
        gamma_base_url: str = "https://gamma-api.polymarket.com",
        kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2",
        kalshi_ticker: str = "KXBTC15M",
    ) -> None:
        self.state = state
        self.coinbase_url = coinbase_url
        self.gamma_base_url = gamma_base_url
        self.kalshi_base_url = kalshi_base_url
        self.kalshi_ticker = kalshi_ticker

    async def fetch_btc_price(self) -> float | None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(self.coinbase_url)
            resp.raise_for_status()
            data = resp.json()
            amount = (data.get("data") or {}).get("amount")
            return float(amount) if amount is not None else None

    def fetch_polymarket(self) -> dict[str, dict[str, Any]]:
        from btc_edge.connectors.poly_client import PolymarketUSClient

        # Polymarket US publishes a single 15-minute BTC up/down market. Resolve
        # full metadata (condition id, token ids, outcome prices, settlement
        # reference) from Gamma and merge the live BBO so ``prob_up`` is never
        # None — the feed no longer goes dark when the book is thin or expired.
        client = PolymarketUSClient()
        out: dict[str, dict[str, Any]] = {}
        res = client.resolve_15m_full()
        prob_up = res.get("prob_up")
        if prob_up is None:
            return out
        up = res.get("up")
        down = res.get("down")
        if up is None:
            up = prob_up
        if down is None:
            down = 1.0 - prob_up
        out["15m"] = {
            "up": up,
            "down": down,
            "prob_up": prob_up,
            "open_strike": res.get("open_strike"),
            "slug": res.get("slug", ""),
            "gamma_slug": res.get("gamma_slug", ""),
            "condition_id": res.get("condition_id", ""),
            "up_token_id": res.get("up_token_id", ""),
            "down_token_id": res.get("down_token_id", ""),
            "reference_source": res.get("reference_source", ""),
            "end_date": res.get("end_date", ""),
        }
        return out

    async def fetch_kalshi(self) -> dict[str, Any]:
        from btc_edge.connectors.kalshi_client import (
            KalshiClient,
            _parse_ts,
            resolve_btc_15m_market,
        )

        client = KalshiClient(signer=None, base_url=self.kalshi_base_url)
        markets = await client.get_btc_markets()
        if not markets:
            return {}

        m = resolve_btc_15m_market(markets)
        if m is None:
            return {}

        bid = to_probability(m.get("yes_bid_dollars"))
        ask = to_probability(m.get("yes_ask_dollars"))
        last = to_probability(m.get("last_price_dollars"))
        prob: float | None = None
        if bid is not None and ask is not None:
            prob = (bid + ask) / 2.0
        elif last is not None:
            prob = last

        return {
            "ticker": m.get("ticker"),
            "prob_up": prob,
            "prob_down": (1.0 - prob) if prob is not None else None,
            "close_time": _parse_ts(m.get("close_time")),
            "open_time": _parse_ts(m.get("open_time")),
            # The BRTI reference at window open (how KXBTC15M actually settles).
            "floor_strike": m.get("floor_strike"),
        }

    async def refresh_once(self) -> None:
        now = time.time()
        self.state.window_countdowns = compute_countdowns(now)

        try:
            price = await self.fetch_btc_price()
            if price is not None:
                self.state.btc_price = price
                self.state.brti_60s = price
                self.state.price_history.append((now, price))
                if len(self.state.price_history) > MAX_HISTORY_POINTS:
                    self.state.price_history = self.state.price_history[-MAX_HISTORY_POINTS:]
        except Exception as exc:  # noqa: BLE001
            logger.debug("BTC price fetch failed: %s", exc)

        try:
            self.state.polymarket_markets = await asyncio.to_thread(self.fetch_polymarket)
            # The window-open reference (open_strike) is the 60s TWAP, owned by the
            # trading loop's _sync_window; do not overwrite it with a stale market
            # field here.
        except Exception as exc:  # noqa: BLE001
            logger.debug("Polymarket fetch failed: %s", exc)

        try:
            self.state.kalshi_market = await self.fetch_kalshi()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Kalshi fetch failed: %s", exc)

        self.state.feed_updated_at = now

    async def run(self, interval: float = 1.0) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                logger.warning("Live feed error: %s", exc)
            await asyncio.sleep(interval)
