"""Kalshi trade-api v2 client.

Implements official REST + WebSocket access with Ed25519 or RSA-PSS request
signing, ``yes_bid_size_fp`` / ``yes_ask_size_fp`` top-of-book parsing, and a
1 req/sec async token bucket for the portfolio/balance endpoint.

Signing rules (2026 spec):
  * Signature payload is ``f"{timestamp_ms}{http_method}{path}"`` where ``path``
    has all query parameters stripped (``?`` and everything after removed).
  * Headers: ``KALSHI-ACCESS-KEY``, ``KALSHI-ACCESS-SIGNATURE``,
    ``KALSHI-ACCESS-TIMESTAMP``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _strip_query(path: str) -> str:
    """Remove ``?...`` from a URL path before signing (CRITICAL per spec)."""
    return path.split("?", 1)[0]


def _client_order_id(ticker: str, side: str, action: str, count: int, price: float) -> str:
    """Unique client order id for idempotency (timestamp-based, 32 hex chars)."""
    import hashlib

    raw = f"{ticker}|{side}|{action}|{count}|{price}|{time.time()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _load_private_key(pem_or_b64: str):
    """Load an Ed25519 or RSA private key from PEM or raw base64.

    Returns a ``cryptography`` private key object. Raises ``ValueError`` with a
    descriptive message if the material is unrecognized.
    """
    from cryptography.hazmat.primitives import serialization

    material = pem_or_b64.strip()
    if not material:
        raise ValueError("empty private key material")

    # PEM path.
    if "-----BEGIN" in material:
        try:
            return serialization.load_pem_private_key(
                material.encode(), password=None
            )
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"failed to load PEM private key: {exc}") from exc

    # Raw base64 path (Ed25519 32-byte seed or PKCS8 DER).
    try:
        raw = base64.b64decode(material, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("private key is neither PEM nor valid base64") from exc

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(raw) == 32:
        try:
            return Ed25519PrivateKey.from_private_bytes(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid Ed25519 seed: {exc}") from exc

    try:
        return serialization.load_der_private_key(raw, password=None)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"failed to load DER private key: {exc}") from exc


@dataclass(slots=True)
class SignedRequest:
    headers: dict[str, str]
    timestamp_ms: int


class KalshiSigner:
    """Signs Kalshi requests with Ed25519 or RSA-PSS.

    ``access_key_id`` is the public ``KALSHI-ACCESS-KEY``. ``private_key`` is
    either PEM or base64 material. Key type is auto-detected.
    """

    def __init__(self, access_key_id: str, private_key: str) -> None:
        self.access_key_id = access_key_id
        self._key = _load_private_key(private_key)

    @property
    def key_type(self) -> str:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        if isinstance(self._key, Ed25519PrivateKey):
            return "ed25519"
        return "rsa"

    def sign(self, method: str, path: str) -> SignedRequest:
        """Produce signed headers for ``method`` + ``path`` (query-stripped)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        clean_path = _strip_query(path)
        timestamp_ms = int(time.time() * 1000)
        message = f"{timestamp_ms}{method.upper()}{clean_path}".encode()

        if isinstance(self._key, Ed25519PrivateKey):
            signature = self._key.sign(message)
        else:
            signature = self._key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )

        headers = {
            "KALSHI-ACCESS-KEY": self.access_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        }
        return SignedRequest(headers=headers, timestamp_ms=timestamp_ms)


class TokenBucket:
    """Async token bucket rate limiter (used for the balance endpoint)."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None) -> None:
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity if capacity is not None else rate_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                await asyncio.sleep(deficit / self.rate)


@dataclass(slots=True)
class OrderBookLevel:
    price: float
    size: float


@dataclass(slots=True)
class KalshiOrderBook:
    market_ticker: str
    best_bid: float
    best_ask: float
    best_bid_size: float
    best_ask_size: float
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

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


class KalshiClient:
    """Async REST client for the Kalshi trade-api v2."""

    def __init__(
        self,
        signer: KalshiSigner | None,
        base_url: str,
        rate_limit_balance_per_sec: float = 1.0,
    ) -> None:
        self.signer = signer
        self.base_url = base_url.rstrip("/")
        # The base URL already contains "/trade-api/v2"; extract that path prefix
        # so the signature covers the FULL path (Kalshi signs the whole path).
        from urllib.parse import urlparse

        self._base_path = urlparse(base_url).path.rstrip("/") or ""
        self._bucket = TokenBucket(rate_limit_balance_per_sec)

    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        if self.signer is None:
            return {}
        return self.signer.sign(method, self._base_path + path).headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import httpx

        headers = self._signed_headers(method, path)
        headers.setdefault("Content-Type", "application/json")
        async with httpx.AsyncClient(base_url=self.base_url, timeout=15.0) as client:
            resp = await client.request(
                method, path, params=params, json=json_body, headers=headers
            )
            resp.raise_for_status()
            return resp.json()

    async def get_balance(self) -> dict[str, Any]:
        """Get portfolio balance, rate-limited to <= 1 req/sec."""
        await self._bucket.acquire(1.0)
        return await self._request("GET", "/portfolio/balance")

    async def intra_account_transfer(
        self,
        amount_centicents: int,
        source_exchange_shard: int = 0,
        destination_exchange_shard: int = 2,
    ) -> dict[str, Any]:
        """Move balance between exchange shards within the same account.

        KXBTC15M settles on exchange shard 2 (the crypto/BTC exchange). The API
        does NOT auto-convert USD the way the web app does, so we explicitly move
        USD (shard 0) -> crypto (shard 2) before placing orders. ``amount`` is an
        integer number of centicents (10_000 == $1.00; 100 == $0.01).
        """
        import uuid

        body = {
            "source": "event_contract",
            "destination": "event_contract",
            "amount": int(amount_centicents),
            "client_transfer_id": str(uuid.uuid4()),
            "source_exchange_shard": int(source_exchange_shard),
            "destination_exchange_shard": int(destination_exchange_shard),
            "source_subaccount": 0,
            "destination_subaccount": 0,
        }
        return await self._request(
            "POST", "/portfolio/intra_exchange_instance_transfer", json_body=body
        )

    async def ensure_crypto_balance(self, min_dollars: float = 5.0) -> bool:
        """Top up the crypto (shard 2) balance from USD (shard 0) if it is low.

        Returns True when a USD -> crypto conversion was submitted, False when the
        crypto balance was already sufficient (or there was no USD to move).
        """
        bal = await self.get_balance()
        usd = 0.0
        crypto = 0.0
        for b in bal.get("balance_breakdown", []) or []:
            idx = b.get("exchange_index")
            amt = float(b.get("balance") or 0.0)
            if idx == 0:
                usd = amt
            elif idx == 2:
                crypto = amt
        if crypto >= min_dollars:
            return False
        need = min_dollars - crypto
        move = min(need, max(0.0, usd - 0.10))
        if move <= 0.0:
            return False
        # ``amount`` is centicents: dollars * 10_000.
        await self.intra_account_transfer(amount_centicents=int(round(move * 10_000.0)))
        return True

    async def get_markets(
        self,
        ticker: str | None = None,
        status: str | None = None,
        series_ticker: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ticker:
            params["tickers"] = ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if limit:
            params["limit"] = limit
        return await self._request("GET", "/markets", params=params)

    async def get_settled_btc_markets(self, limit: int = 200) -> list[dict[str, Any]]:
        """Fetch settled KXBTC15M markets (real resolved BTC up/down windows)."""
        data = await self.get_markets(series_ticker="KXBTC15M", status="settled", limit=limit)
        return data.get("markets", []) if isinstance(data, dict) else []

    async def get_btc_markets(self) -> list[dict[str, Any]]:
        """Fetch the KXBTC15M series (15-minute BTC up/down markets)."""
        data = await self._request(
            "GET", "/markets", params={"series_ticker": "KXBTC15M", "limit": 200}
        )
        return data.get("markets", []) if isinstance(data, dict) else []

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{ticker}/orderbook")

    async def place_order(
        self,
        ticker: str,
        side: str,  # yes | no
        action: str,  # buy | sell
        count: int,
        price: float,  # dollars (0..1)
        order_type: str = "limit",
        post_only: bool = False,
    ) -> dict[str, Any]:
        # Convert legacy (side=yes/no, action=buy/sell) to V2 (side=bid/ask).
        # "bid" = buy YES (long yes); "ask" = sell YES (long no). Price is quoted
        # in YES-leg fixed-point dollars.
        long_yes = (action == "buy" and side == "yes") or (action == "sell" and side == "no")
        v2_side = "bid" if long_yes else "ask"
        yes_price = price if side == "yes" else (1.0 - price)
        body = {
            "ticker": ticker,
            "side": v2_side,
            "count": str(count),
            "price": f"{yes_price:.4f}",
            "time_in_force": "good_till_canceled" if order_type == "limit" else "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": _client_order_id(ticker, side, action, count, price),
        }
        return await self._request("POST", "/portfolio/events/orders", json_body=body)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def cancel_all_orders(self) -> dict[str, Any]:
        return await self._request("DELETE", "/portfolio/orders")

    async def get_orders(self, ticker: str | None = None) -> dict[str, Any]:
        params = {"ticker": ticker} if ticker else None
        return await self._request("GET", "/portfolio/orders", params=params)

    async def get_fills(
        self,
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict[str, Any]:
        """Fetch personal filled-order history (trade history).

        Used to review past trades for patterns/bias and to build a validated
        record for the model. Paginate with ``cursor`` from a prior response.
        """
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await self._request("GET", "/portfolio/fills", params=params)

    async def get_settlements(self, ticker: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Fetch settled positions with realized PnL (how past trades actually did)."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/portfolio/settlements", params=params)

    async def get_positions(self, limit: int = 100) -> dict[str, Any]:
        """Fetch open positions (unsettled contracts currently held)."""
        return await self._request("GET", "/portfolio/positions", params={"limit": limit})


def parse_orderbook(payload: dict[str, Any], ticker: str) -> KalshiOrderBook:
    """Parse a Kalshi orderbook payload into a normalized structure.

    Uses ``yes_bid_size_fp`` / ``yes_ask_size_fp`` for top-of-book sizes (the
    legacy integer size fields return 0.0000 and must not be used).
    """
    book = payload.get("orderbook", payload)
    bids_raw = book.get("yes_bid", []) or []
    asks_raw = book.get("yes_ask", []) or []

    def _level(lvl: dict) -> OrderBookLevel:
        return OrderBookLevel(
            price=float(lvl.get("price", 0)), size=float(lvl.get("size", 0))
        )

    bids = [_level(lvl) for lvl in bids_raw]
    asks = [_level(lvl) for lvl in asks_raw]

    best_bid = bids[0].price if bids else 0.0
    best_ask = asks[0].price if asks else 1.0
    best_bid_size = float(book.get("yes_bid_size_fp", 0.0))
    best_ask_size = float(book.get("yes_ask_size_fp", 0.0))

    return KalshiOrderBook(
        market_ticker=ticker,
        best_bid=best_bid,
        best_ask=best_ask,
        best_bid_size=best_bid_size,
        best_ask_size=best_ask_size,
        bids=bids,
        asks=asks,
    )


class KalshiWSClient:
    """WebSocket client for Kalshi orderbook feeds.

    Subscribes to the ``orderbook_delta`` channel for a ticker and yields
    parsed ``KalshiOrderBook`` snapshots. Authentication is optional for public
    market data; pass a signer to subscribe to authenticated channels.
    """

    def __init__(self, ws_url: str, ticker: str) -> None:
        self.ws_url = ws_url
        self.ticker = ticker
        self._running = False
        self.last_message_ts: float = 0.0

    async def subscribe(self, on_book) -> None:
        """Subscribe and invoke ``on_book(book: KalshiOrderBook)`` on updates."""
        import websockets

        self._running = True
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    sub = {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {"channels": ["orderbook_delta"], "market_tickers": [self.ticker]},
                    }
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        self.last_message_ts = time.time()
                        msg = json.loads(raw)
                        if msg.get("type") == "orderbook_snapshot":
                            book = parse_orderbook(msg.get("msg", msg), self.ticker)
                            on_book(book)
                        elif msg.get("type") == "orderbook_delta":
                            # Delta messages contain incremental updates; the
                            # client re-fetches the full book via REST when a
                            # delta arrives (kept simple + reliable).
                            on_book(msg.get("msg", msg))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Kalshi WS error: %s; reconnecting", exc)
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self._running = False


def build_kalshi_signer_from_env() -> KalshiSigner | None:
    """Build a signer from ``KALSHI_ACCESS_KEY_ID`` / ``KALSHI_PRIVATE_KEY`` env.

    Reads the OS environment first, then falls back to the ``Settings`` object
    (which loads ``.env`` via pydantic-settings, since ``os.getenv`` does not
    see ``.env``).
    """
    import os

    key_id = os.getenv("KALSHI_ACCESS_KEY_ID") or os.getenv("KALSHI_API_KEY_ID")
    private = os.getenv("KALSHI_PRIVATE_KEY")
    if not key_id or not private:
        from btc_edge.config import get_settings

        s = get_settings()
        key_id = key_id or s.kalshi_access_key_id or s.kalshi_api_key_id
        private = private or s.kalshi_private_key
    if not key_id or not private:
        return None
    return KalshiSigner(key_id, private)


def resolve_btc_markets(
    markets: list[dict[str, Any]], current_price: float, now_ts: float | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Pick the nearest "above" (``strike_type == "greater"``) and "below"
    (``strike_type == "less"``) BTC price-range markets to ``current_price``,
    skipping any market whose ``close_time`` has already passed.

    Returns ``(above_market, below_market)`` — either may be ``None``.
    """
    from datetime import datetime

    now = now_ts if now_ts is not None else time.time()
    above: dict[str, Any] | None = None
    below: dict[str, Any] | None = None
    for m in markets:
        stype = m.get("strike_type")
        strike = m.get("floor_strike") if stype == "greater" else m.get("cap_strike")
        if strike is None:
            continue
        strike = float(strike)

        # Skip markets that have already closed/settled or aren't open yet.
        status = (m.get("status") or "").lower()
        if status in {"closed", "finalized", "settled", "initialized"}:
            continue

        close = m.get("close_time")
        if close:
            try:
                close_ts = datetime.fromisoformat(str(close).replace("Z", "+00:00")).timestamp()
                if close_ts < now:
                    continue
            except (ValueError, TypeError):
                pass

        if stype == "greater":
            ref = float(above.get("floor_strike") or 0) if above else None
            if above is None or (ref is not None and abs(strike - current_price) < abs(ref - current_price)):
                above = m
        elif stype == "less":
            ref = float(below.get("cap_strike") or 0) if below else None
            if below is None or (ref is not None and abs(strike - current_price) < abs(ref - current_price)):
                below = m
    return above, below


def _parse_ts(value: Any) -> float | None:
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def resolve_btc_15m_market(
    markets: list[dict[str, Any]], now_ts: float | None = None
) -> dict[str, Any] | None:
    """Pick the current (open) KXBTC15M 15-minute window, else the nearest future one.

    ``KXBTC15M`` markets are "BTC price up in next 15 mins?" — a pure up/down
    binary (no strike). Return the market whose ``[open_time, close_time)``
    contains now; if none is open yet, return the soonest-to-open window.
    """
    now = now_ts if now_ts is not None else time.time()
    active: dict[str, Any] | None = None
    future: dict[str, Any] | None = None
    for m in markets:
        open_ts = _parse_ts(m.get("open_time"))
        close_ts = _parse_ts(m.get("close_time"))
        if open_ts is not None and close_ts is not None and open_ts <= now < close_ts:
            active = m
            break
        if open_ts is not None and open_ts > now and (
            future is None or open_ts < (_parse_ts(future.get("open_time")) or float("inf"))
        ):
            future = m
    return active or future


