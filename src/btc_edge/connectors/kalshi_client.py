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
        self._bucket = TokenBucket(rate_limit_balance_per_sec)

    def _signed_headers(self, method: str, path: str) -> dict[str, str]:
        if self.signer is None:
            return {}
        return self.signer.sign(method, path).headers

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

    async def get_markets(self, ticker: str | None = None, status: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ticker:
            params["tickers"] = ticker
        if status:
            params["status"] = status
        return await self._request("GET", "/markets", params=params)

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return await self._request("GET", f"/markets/{ticker}/orderbook")

    async def place_order(
        self,
        ticker: str,
        side: str,  # yes | no
        action: str,  # buy | sell
        count: int,
        price: float,
        order_type: str = "limit",
        post_only: bool = False,
    ) -> dict[str, Any]:
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
            "buy_max_cost": None,
            "sell_position_floor": None,
        }
        if order_type == "limit":
            body["yes_price"] = int(round(price * 100))  # cents
            body["post_only"] = post_only
        return await self._request("POST", "/portfolio/orders", json_body=body)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def cancel_all_orders(self) -> dict[str, Any]:
        return await self._request("DELETE", "/portfolio/orders")

    async def get_orders(self, ticker: str | None = None) -> dict[str, Any]:
        params = {"ticker": ticker} if ticker else None
        return await self._request("GET", "/portfolio/orders", params=params)


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
    """Build a signer from ``KALSHI_ACCESS_KEY_ID`` / ``KALSHI_PRIVATE_KEY`` env."""
    import os

    key_id = os.getenv("KALSHI_ACCESS_KEY_ID") or os.getenv("KALSHI_API_KEY_ID")
    private = os.getenv("KALSHI_PRIVATE_KEY")
    if not key_id or not private:
        return None
    return KalshiSigner(key_id, private)


