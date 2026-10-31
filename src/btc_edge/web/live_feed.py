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

from btc_edge.connectors.poly_client import compute_window_ts, next_window_ts, slug_for
from btc_edge.web.app import EngineState

logger = logging.getLogger(__name__)

COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
TIMEFRAMES: tuple[tuple[int, str], ...] = ((300, "5m"), (900, "15m"))
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
        from btc_edge.connectors.poly_client import GammaClient

        gamma = GammaClient(self.gamma_base_url)
        out: dict[str, dict[str, Any]] = {}
        for tf, label in TIMEFRAMES:
            window_ts = compute_window_ts(time.time(), tf)
            slug = slug_for(tf, window_ts)
            try:
                res = gamma.resolve_market(slug, tf, window_ts)
            except Exception as exc:  # noqa: BLE001 - best-effort per-market
                logger.debug("Polymarket %s unavailable: %s", label, exc)
                continue
            if res is None:
                continue
            prices = res.outcome_prices or []
            up = float(prices[0]) if len(prices) > 0 else 0.0
            down = float(prices[1]) if len(prices) > 1 else 0.0
            if up <= 0 and down <= 0:
                continue
            out[label] = {
                "up": up,
                "down": down,
                "prob_up": up,
                "open_strike": res.open_reference_price,
            }
        return out

    async def fetch_kalshi(self) -> dict[str, Any]:
        from btc_edge.connectors.kalshi_client import KalshiClient

        client = KalshiClient(signer=None, base_url=self.kalshi_base_url)
        data = await client.get_markets(ticker=self.kalshi_ticker)
        markets = data.get("markets", [])
        if not markets:
            return {}
        m = markets[0]
        yes_bid = to_probability(m.get("yes_bid"))
        yes_ask = to_probability(m.get("yes_ask"))
        last = to_probability(m.get("last_price"))
        prob: float | None = None
        if yes_bid is not None and yes_ask is not None:
            prob = (yes_bid + yes_ask) / 2.0
        elif last is not None:
            prob = last
        return {
            "last_price": last,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "prob_up": prob,
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
            pm5 = self.state.polymarket_markets.get("5m")
            if pm5 and pm5.get("open_strike") is not None:
                self.state.open_strike = float(pm5["open_strike"])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Polymarket fetch failed: %s", exc)

        try:
            self.state.kalshi_market = await self.fetch_kalshi()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Kalshi fetch failed: %s", exc)

        self.state.feed_updated_at = now

    async def run(self, interval: float = 3.0) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                logger.warning("Live feed error: %s", exc)
            await asyncio.sleep(interval)
