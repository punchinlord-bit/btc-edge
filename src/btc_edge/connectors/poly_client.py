"""Polymarket connector.

Wraps the 2026 ``polymarket-client`` SDK (``PublicClient``,
``AsyncSecureClient``) and implements deterministic market discovery via the
Gamma API. Slug construction is deterministic on the window timestamp:

    window_ts = (int(time.time()) // timeframe_seconds) * timeframe_seconds
    slug      = f"btc-updown-{minutes}m-{window_ts}"

Discovery queries ``https://gamma-api.polymarket.com/events?slug=...`` and
extracts the Up/Down conditional token IDs, condition ID, and reference open
price. The CLOB WebSocket streams L2 orderbook updates at <= 1s cadence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def compute_window_ts(now_ts: float, timeframe_seconds: int) -> int:
    """Floor the current time to the window boundary for the timeframe."""
    return (int(now_ts) // int(timeframe_seconds)) * int(timeframe_seconds)


def slug_for(timeframe_seconds: int, window_ts: int) -> str:
    minutes = int(timeframe_seconds) // 60
    return f"btc-updown-{minutes}m-{window_ts}"


def next_window_ts(now_ts: float, timeframe_seconds: int) -> int:
    return compute_window_ts(now_ts, timeframe_seconds) + timeframe_seconds


@dataclass(slots=True)
class MarketResolution:
    slug: str
    timeframe_seconds: int
    window_ts: int
    condition_id: str
    token_ids: list[str]  # [up_token_id, down_token_id] order per outcomes
    outcomes: list[str]
    outcome_prices: list[float]
    open_reference_price: float | None = None

    @property
    def up_token_id(self) -> str:
        return self.token_ids[0] if self.token_ids else ""

    @property
    def down_token_id(self) -> str:
        return self.token_ids[1] if len(self.token_ids) > 1 else ""

    def price_of(self, outcome: str) -> float:
        outcome = outcome.lower()
        for i, o in enumerate(self.outcomes):
            if o.lower() == outcome and i < len(self.outcome_prices):
                return self.outcome_prices[i]
        return 0.0


class GammaClient:
    """Minimal synchronous client for the Polymarket Gamma discovery API."""

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com") -> None:
        self.base_url = base_url.rstrip("/")

    def get_events(self, slug: str) -> list[dict]:
        import requests

        url = f"{self.base_url}/events"
        resp = requests.get(url, params={"slug": slug}, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("events", data.get("data", []))
        return data if isinstance(data, list) else []

    def resolve_market(self, slug: str, timeframe_seconds: int, window_ts: int) -> MarketResolution | None:
        events = self.get_events(slug)
        for event in events:
            for market in event.get("markets", []):
                condition_id = market.get("conditionId") or market.get("condition_id") or ""
                clob_tokens = market.get("clobTokenIds") or market.get("clob_token_ids")
                outcomes_raw = market.get("outcomes") or market.get("outcomeNames")
                prices_raw = market.get("outcomePrices") or market.get("outcome_prices")
                if not condition_id:
                    continue

                token_ids = _parse_json_list(clob_tokens)
                outcomes = _parse_json_list(outcomes_raw)
                prices = [float(p) for p in _parse_json_list(prices_raw)]

                open_ref = None
                ref_price = market.get("referencePrice")
                if ref_price is not None:
                    open_ref = float(ref_price)

                return MarketResolution(
                    slug=slug,
                    timeframe_seconds=timeframe_seconds,
                    window_ts=window_ts,
                    condition_id=condition_id,
                    token_ids=token_ids,
                    outcomes=[str(o) for o in outcomes],
                    outcome_prices=prices,
                    open_reference_price=open_ref,
                )
        return None


def _parse_json_list(value) -> list[str]:
    """Parse a value that may be a JSON string or already a list."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return [value]
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


@dataclass(slots=True)
class PolyBookLevel:
    price: float
    size: float


@dataclass(slots=True)
class PolyOrderBook:
    token_id: str
    bids: list[PolyBookLevel] = field(default_factory=list)
    asks: list[PolyBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    def imbalance(self, levels: int = 3) -> float:
        bid_depth = sum(lvl.size for lvl in self.bids[:levels])
        ask_depth = sum(lvl.size for lvl in self.asks[:levels])
        total = bid_depth + ask_depth
        if total <= 0:
            return 0.0
        return (bid_depth - ask_depth) / total


class PolymarketClient:
    """Unified Polymarket wrapper: Gamma discovery + SDK clients.

    ``PublicClient`` is used for public market data and ``AsyncSecureClient``
    for authenticated trading (created lazily from a wallet private key).
    """

    def __init__(
        self,
        gamma: GammaClient,
        wallet: str | None = None,
        private_key: str | None = None,
    ) -> None:
        self.gamma = gamma
        self.wallet = wallet
        self.private_key = private_key
        self._public: Any = None
        self._secure: Any = None

    @property
    def public_client(self):
        if self._public is None:
            from polymarket import PublicClient

            self._public = PublicClient()
        return self._public

    async def secure_client(self):
        if self._secure is None:
            if not self.private_key:
                raise ValueError("POLYMARKET_PRIVATE_KEY is required for secure client")
            from polymarket import AsyncSecureClient

            self._secure = await AsyncSecureClient.create(
                private_key=self.private_key,
                wallet=self.wallet,
            )
        return self._secure

    def resolve_5m_15m(self, now_ts: float | None = None) -> dict[int, MarketResolution | None]:
        """Resolve the current 5m/15m markets deterministically."""
        now = now_ts if now_ts is not None else time.time()
        out: dict[int, MarketResolution | None] = {}
        for tf in (300, 900):
            window_ts = compute_window_ts(now, tf)
            slug = slug_for(tf, window_ts)
            out[tf] = self.gamma.resolve_market(slug, tf, window_ts)
        return out

    async def close(self) -> None:
        import contextlib

        if self._secure is not None:
            with contextlib.suppress(Exception):
                await self._secure.close()


class PolymarketCLOBWS:
    """CLOB WebSocket client streaming L2 orderbook updates for a token."""

    def __init__(self, ws_url: str, token_id: str) -> None:
        self.ws_url = ws_url
        self.token_id = token_id
        self._running = False
        self.last_message_ts: float = 0.0

    async def subscribe(self, on_book) -> None:
        import websockets

        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    sub = {
                        "type": "market",
                        "assets_ids": [self.token_id],
                    }
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        self.last_message_ts = time.time()
                        msg = json.loads(raw)
                        on_book(msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Polymarket CLOB WS error: %s; reconnecting", exc)
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self._running = False

