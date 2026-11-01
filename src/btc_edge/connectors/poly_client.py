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
    resolution_source: str = ""
    end_date: str = ""
    question: str = ""

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

                token_ids = [_to_hex_token_id(t) for t in _parse_json_list(clob_tokens)]
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
                    resolution_source=str(market.get("resolutionSource") or ""),
                    end_date=str(market.get("endDate") or market.get("end_date") or ""),
                    question=str(market.get("question") or ""),
                )
        return None


def _to_hex_token_id(value: str) -> str:
    """Normalize a Polymarket token id to a 0x-prefixed 64-hex string.

    Gamma's ``clobTokenIds`` arrives as JSON-encoded *decimal* strings; the CLOB
    order/websocket layers expect 256-bit hex ids. Values already hex-prefixed
    (``0x…``) pass through unchanged.
    """
    v = str(value).strip()
    if v.startswith("0x") or v.startswith("0X"):
        return v
    if not v:
        return ""
    try:
        return "0x" + format(int(v, 10), "064x")
    except ValueError:
        # Not decimal; try hex, otherwise return the raw string untouched.
        try:
            return "0x" + format(int(v, 16), "064x")
        except ValueError:
            return v


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


def btc_updown_15m_slug(now_ts: float | None = None) -> str:
    """Current Polymarket US ``BTC Up or Down 15m`` market slug.

    Polymarket US (polymarket.us) lists short-horizon BTC up/down markets with
    slugs of the form ``cpc-btc-updown-15m-{YYYY-MM-DD-HHMM}z`` where the timestamp
    is the **Eastern Time** window open floored to the 15-minute boundary (the "z"
    suffix is cosmetic — the wall clock is US/Eastern, not UTC).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = datetime.now(et) if now_ts is None else datetime.fromtimestamp(now_ts, et)
    window = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return f"cpc-btc-updown-15m-{window.strftime('%Y-%m-%d-%H%M')}z"


def _order_intent(side: str, action: str) -> str:
    """Map a (side, action) decision to a Polymarket US order intent."""
    if side == "up" and action == "buy":
        return "ORDER_INTENT_BUY_LONG"
    if side == "up" and action == "sell":
        return "ORDER_INTENT_SELL_LONG"
    if side == "down" and action == "buy":
        return "ORDER_INTENT_BUY_SHORT"
    if side == "down" and action == "sell":
        return "ORDER_INTENT_SELL_SHORT"
    raise ValueError(f"unsupported side/action: {side}/{action}")


class PolymarketUSClient:
    """Polymarket US connector.

    Wraps the *synchronous* ``polymarket-us`` SDK behind async methods so it
    drops into the existing asyncio engine. Auth is a Key ID + Secret Key issued
    from ``polymarket.us/developer`` (NOT the EVM private key used on
    polymarket.com). Trades the ``BTC Up or Down 15m`` market.
    """

    def __init__(self, key_id: str | None = None, secret_key: str | None = None) -> None:
        self.key_id = key_id
        self.secret_key = secret_key
        self._client: Any = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from polymarket_us import PolymarketUS

            self._client = PolymarketUS(key_id=self.key_id, secret_key=self.secret_key)
        return self._client

    @property
    def available(self) -> bool:
        return bool(self.key_id and self.secret_key)

    @staticmethod
    def _px(value: Any) -> float | None:
        try:
            v = float((value or {}).get("value"))
        except (AttributeError, TypeError, ValueError):
            return None
        return v

    def resolve_15m(self, now_ts: float | None = None) -> dict[str, Any]:
        """Resolve the current 15m market: slug + best bid/ask/mid (best-effort)."""
        slug = btc_updown_15m_slug(now_ts)
        out: dict[str, Any] = {"slug": slug, "bid": None, "ask": None, "mid": None}
        try:
            bbo = self.client.markets.bbo(slug)
            data = bbo.get("marketData") or {}
            out["bid"] = self._px(data.get("bestBid"))
            out["ask"] = self._px(data.get("bestAsk"))
            out["mid"] = self._px(data.get("currentPx"))
        except Exception as exc:  # noqa: BLE001 - market reads are best-effort
            logger.debug("Polymarket US bbo unavailable: %s", exc)
        return out

    def resolve_15m_full(self, now_ts: float | None = None) -> dict[str, Any]:
        """Resolve the current 15m market with full metadata + live BBO.

        Combines Gamma discovery (``btc-updown-15m-{epoch}`` slug → condition id,
        token ids, outcome prices, settlement reference) with the live BBO so the
        feed always has a non-null up/down probability. ``prob_up`` prefers the
        live BBO mid and falls back through ask/bid to Gamma's outcome prices, so
        the feed never goes dark when the book is thin or the market has expired.
        """
        now = now_ts if now_ts is not None else time.time()
        window_ts = compute_window_ts(now, 900)
        gamma_slug = slug_for(900, window_ts)

        resolution = GammaClient().resolve_market(gamma_slug, 900, window_ts)
        bbo = self.resolve_15m(now_ts)
        et_slug = bbo.get("slug") or gamma_slug

        up_price: float | None = None
        down_price: float | None = None
        token_ids: list[str] = []
        condition_id = ""
        outcomes: list[str] = []
        end_date = ""
        reference_source = ""
        question = ""
        if resolution is not None:
            condition_id = resolution.condition_id
            token_ids = resolution.token_ids
            outcomes = resolution.outcomes
            end_date = resolution.end_date
            reference_source = resolution.resolution_source
            question = resolution.question
            if len(resolution.outcome_prices) >= 2:
                up_price = resolution.outcome_prices[0]
                down_price = resolution.outcome_prices[1]
            elif resolution.outcome_prices:
                up_price = resolution.outcome_prices[0]

        # Gamma outcome prices are the authoritative, always-current market
        # probability. The BBO currentPx can be stale (it reports the expired
        # window's settled price right at rollover), so prefer Gamma and fall
        # back through the live book only when Gamma is unavailable.
        prob_up = up_price
        if prob_up is None:
            prob_up = bbo.get("mid")
        if prob_up is None:
            prob_up = bbo.get("ask")
        if prob_up is None:
            prob_up = bbo.get("bid")

        return {
            "slug": et_slug,
            "gamma_slug": gamma_slug,
            "condition_id": condition_id,
            "up_token_id": token_ids[0] if token_ids else "",
            "down_token_id": token_ids[1] if len(token_ids) > 1 else "",
            "outcomes": outcomes,
            "up": up_price,
            "down": down_price,
            "prob_up": prob_up,
            "bid": bbo.get("bid"),
            "ask": bbo.get("ask"),
            "mid": bbo.get("mid"),
            "open_strike": None,
            "reference_source": reference_source,
            "end_date": end_date,
            "question": question,
        }

    async def get_bbo(self, now_ts: float | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self.resolve_15m, now_ts)

    def _place_order_sync(
        self, slug: str, side: str, action: str, price: float, size: float, is_maker: bool
    ) -> Any:
        intent = _order_intent(side, action)
        # ``price`` is the decision's YES ("up") price in [0,1]; short (down)
        # orders trade the NO leg at 1 - yes_price.
        px = price if side == "up" else (1.0 - price)
        px = max(0.01, min(0.99, round(px, 2)))
        quantity = max(1, int(round(size)))
        tif = "TIME_IN_FORCE_GOOD_TILL_CANCEL" if is_maker else "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
        params = {
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{px:.2f}", "currency": "USD"},
            "quantity": quantity,
            "tif": tif,
        }
        resp = self.client.orders.create(params)
        # Polymarket returns HTTP 200 even for orders it does NOT accept — the
        # terminal state lives in ``executions[].type``. Surface any terminal
        # non-fill state (REJECTED / CANCELED / EXPIRED / DONE_FOR_DAY) as an
        # error so the trading loop does not record a phantom "fill" (and Telegram
        # alert) for an order that never landed on the venue.
        executions = resp.get("executions") if isinstance(resp, dict) else []
        terminal_fail = {
            "EXECUTION_TYPE_REJECTED",
            "EXECUTION_TYPE_CANCELED",
            "EXECUTION_TYPE_EXPIRED",
            "EXECUTION_TYPE_DONE_FOR_DAY",
        }
        for ex in executions or []:
            ex_type = ex.get("type")
            if ex_type in terminal_fail:
                reason = ex.get("orderRejectReason") or ex.get("text") or ex_type
                raise RuntimeError(f"Polymarket order not accepted ({ex_type}): {reason}")
        states = [ex.get("type") for ex in (executions or [])] or ["(resting)"]
        logger.info(
            "Polymarket order submitted: %s %s %s qty=%d px=%.2f -> %s",
            action, side, slug, quantity, px, states,
        )
        return resp

    async def place_order(
        self, slug: str, side: str, action: str, price: float, size: float, is_maker: bool = False
    ) -> Any:
        return await asyncio.to_thread(self._place_order_sync, slug, side, action, price, size, is_maker)

    def _balance_sync(self) -> float:
        bal = self.client.account.balances()
        for b in bal.get("balances") or []:
            if b.get("currency") == "USD":
                return float(b.get("currentBalance") or 0.0)
        return 0.0

    async def get_balance(self) -> float:
        return await asyncio.to_thread(self._balance_sync)

    def _positions_sync(self) -> list[dict[str, Any]]:
        resp = self.client.portfolio.positions()
        if not isinstance(resp, dict):
            return []
        positions = resp.get("positions") or {}
        if isinstance(positions, dict):
            return list(positions.values())
        return list(positions)

    async def get_positions(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._positions_sync)

    def _activities_sync(self) -> list[dict[str, Any]]:
        resp = self.client.portfolio.activities()
        if not isinstance(resp, dict):
            return []
        return list(resp.get("activities") or [])

    async def get_activities(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._activities_sync)

