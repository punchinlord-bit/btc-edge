"""Bybit & Deribit connectors.

Bybit provides perpetual funding rate, open interest, and liquidation feeds
(used by the regime classifier). Deribit provides the BTC index price and the
DVOL implied-volatility index (used as the ATM IV input to the ``N(d2)``
option-implied probability model).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeribitSnapshot:
    btc_index: float
    dvol: float  # annualized implied vol (fraction, e.g. 0.45 = 45%)
    ts: float


@dataclass(slots=True)
class BybitSnapshot:
    funding_rate: float
    funding_rate_zscore: float
    open_interest: float
    liquidation_spike: bool
    ts: float


class DeribitClient:
    """Synchronous REST client for Deribit public market data."""

    def __init__(self, rest_url: str = "https://www.deribit.com/api/v2", index: str = "btc_usd") -> None:
        self.rest_url = rest_url.rstrip("/")
        self.index = index

    def _get(self, method: str, params: dict | None = None) -> dict:
        import requests

        url = f"{self.rest_url}/{method}"
        resp = requests.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    def get_btc_index(self) -> float:
        data = self._get("public/get_index_price", {"index_name": self.index})
        result = data.get("result", {})
        return float(result.get("index_price", 0.0))

    def get_dvol(self) -> float:
        """Return the current DVOL (30-day implied vol index) as a fraction."""
        data = self._get(
            "public/get_volatility_index_data",
            {"currency": "BTC", "resolution": "1D"},
        )
        result = data.get("result", {})
        points = result.get("data", [])
        if not points:
            return 0.0
        # Latest point holds the most recent DVOL close.
        latest = points[-1]
        if isinstance(latest, list):
            # DVOL series: [timestamp_ms, open, high, low, close]
            return float(latest[4]) / 100.0 if len(latest) >= 5 else 0.0
        if isinstance(latest, dict):
            return float(latest.get("close", 0.0)) / 100.0
        return 0.0

    def snapshot(self) -> DeribitSnapshot:
        btc = self.get_btc_index()
        dvol = self.get_dvol()
        return DeribitSnapshot(btc_index=btc, dvol=dvol, ts=time.time())


class FundingRateBuffer:
    """Rolling funding-rate buffer with running mean/std for Z-score."""

    def __init__(self, maxlen: int = 600) -> None:
        self._rates: list[float] = []
        self.maxlen = maxlen

    def add(self, rate: float) -> None:
        self._rates.append(rate)
        if len(self._rates) > self.maxlen:
            self._rates = self._rates[-self.maxlen:]

    def zscore(self, rate: float) -> float:
        if len(self._rates) < 2:
            return 0.0
        mean = sum(self._rates) / len(self._rates)
        var = sum((r - mean) ** 2 for r in self._rates) / len(self._rates)
        std = var ** 0.5
        if std <= 0:
            return 0.0
        return (rate - mean) / std

    def clear(self) -> None:
        self._rates.clear()


class BybitWSClient:
    """WebSocket client for Bybit funding / OI / liquidation public feed."""

    def __init__(
        self,
        ws_url: str = "wss://stream.bybit.com/v5/public/linear",
        symbol: str = "BTCUSDT",
    ) -> None:
        self.ws_url = ws_url
        self.symbol = symbol
        self.funding = FundingRateBuffer()
        self.last_funding_rate: float = 0.0
        self.open_interest: float = 0.0
        self.liquidation_spike: bool = False
        self.last_message_ts: float = 0.0
        self._running = False
        self._recent_liqs: list[float] = []

    def _handle(self, msg: dict) -> None:
        self.last_message_ts = time.time()
        topic = msg.get("topic", "")
        data = msg.get("data", {})
        if "tickers" in topic:
            fr = float(data.get("fundingRate", 0.0))
            self.last_funding_rate = fr
            self.funding.add(fr)
        elif "tickers" in topic and data.get("openInterest") is not None:
            self.open_interest = float(data.get("openInterest", 0.0))
        elif "liquidation" in topic:
            self._recent_liqs.append(time.time())
            cutoff = time.time() - 30.0
            self._recent_liqs = [t for t in self._recent_liqs if t >= cutoff]
            self.liquidation_spike = len(self._recent_liqs) >= 3

    def snapshot(self) -> BybitSnapshot:
        return BybitSnapshot(
            funding_rate=self.last_funding_rate,
            funding_rate_zscore=self.funding.zscore(self.last_funding_rate),
            open_interest=self.open_interest,
            liquidation_spike=self.liquidation_spike,
            ts=self.last_message_ts,
        )

    async def run(self) -> None:
        import websockets

        self._running = True
        sub = {
            "op": "subscribe",
            "args": [
                f"tickers.{self.symbol}",
                f"liquidation.{self.symbol}",
            ],
        }
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    await ws.send(json.dumps(sub))
                    async for raw in ws:
                        self._handle(json.loads(raw))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Bybit WS error: %s; reconnecting", exc)
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        self._running = False
