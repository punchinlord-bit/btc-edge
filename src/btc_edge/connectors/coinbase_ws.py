"""Coinbase BTC-USD connector: tick feed, candle builder, and the 60-second
BRTI rolling settlement proxy.

Both Kalshi ``KXBTC15M`` and Polymarket ``btc-updown-5m-*`` settle on a
60-second average reference price against the window-open reference. This module
maintains a sub-second rolling 60-second TWAP/VWAP buffer so the engine knows,
second by second during the final 60 seconds, how much of the settlement average
is already locked in.

All pure-python math (``RollingPriceBuffer``, ``CandleBuilder``,
``SettlementTracker``) is fully decoupled from the WebSocket so it is trivially
unit-testable and reusable by the offline synthetic generator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Tick:
    ts: float
    price: float
    size: float
    side: str = "buy"  # buy | sell

    def __post_init__(self) -> None:
        self.price = float(self.price)
        self.size = float(self.size)


@dataclass(slots=True)
class Candle:
    open_ts: float
    timeframe: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_ts(self) -> float:
        return self.open_ts + self.timeframe


class RollingPriceBuffer:
    """Rolling window of trades with TWAP and VWAP."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = float(window_seconds)
        self._samples: deque[tuple[float, float]] = deque()  # (ts, price)
        self._trades: deque[Tick] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        while self._trades and self._trades[0].ts < cutoff:
            self._trades.popleft()

    def add(self, tick: Tick) -> None:
        self._samples.append((tick.ts, tick.price))
        self._trades.append(tick)
        self._prune(tick.ts)

    def add_price(self, ts: float, price: float) -> None:
        self.add(Tick(ts=ts, price=price, size=0.0, side=""))

    def clear(self) -> None:
        self._samples.clear()
        self._trades.clear()

    def twap(self) -> float | None:
        if not self._samples:
            return None
        return sum(p for _, p in self._samples) / len(self._samples)

    def vwap(self) -> float | None:
        total_notional = 0.0
        total_size = 0.0
        for t in self._trades:
            if t.size > 0:
                total_notional += t.price * t.size
                total_size += t.size
        if total_size <= 0:
            return self.twap()
        return total_notional / total_size

    def last_price(self) -> float | None:
        return self._samples[-1][1] if self._samples else None

    def __len__(self) -> int:
        return len(self._samples)


class SettlementTracker:
    """Computes the locked-in 60-second settlement trajectory for a window.

    A window settles on the mean of ``settle_seconds`` (default 60) of price
    samples between ``settle_start_ts`` and ``settle_end_ts``. During the final
    ``settle_seconds`` the tracker blends already-elapsed samples (locked) with
    the current spot to estimate the final settlement price in real time.
    """

    def __init__(self, settle_seconds: float = 60.0) -> None:
        self.settle_seconds = float(settle_seconds)
        self._locked: list[tuple[float, float]] = []  # (ts, price)

    def feed(self, ts: float, price: float, settle_start_ts: float, settle_end_ts: float) -> None:
        if settle_start_ts <= ts < settle_end_ts:
            self._locked.append((ts, price))
            self._locked = [(t, p) for t, p in self._locked if t >= settle_start_ts]

    def reset(self) -> None:
        self._locked.clear()

    def locked_average(self) -> float | None:
        if not self._locked:
            return None
        return sum(p for _, p in self._locked) / len(self._locked)

    def settlement_estimate(self, now: float, settle_start_ts: float, settle_end_ts: float) -> float:
        elapsed = max(0.0, min(now, settle_end_ts) - settle_start_ts)
        total = self.settle_seconds
        remaining = max(0.0, total - elapsed)

        locked = self.locked_average()
        last = self._locked[-1][1] if self._locked else None
        if locked is None:
            return last if last is not None else 0.0

        blended = (locked * elapsed + (last if last is not None else locked) * remaining) / total
        return blended


class CandleBuilder:
    """Aggregates ticks into fixed-timeframe candles (e.g. 60s / 300s)."""

    def __init__(self, timeframe: int) -> None:
        self.timeframe = timeframe
        self._current: Candle | None = None
        self._finished: list[Candle] = []

    def _bucket_ts(self, ts: float) -> float:
        return (int(ts) // self.timeframe) * self.timeframe

    def add_tick(self, tick: Tick) -> None:
        bucket = self._bucket_ts(tick.ts)
        if self._current is None:
            self._current = Candle(
                open_ts=bucket, timeframe=self.timeframe, open=tick.price,
                high=tick.price, low=tick.price, close=tick.price, volume=tick.size,
            )
        elif bucket == self._current.open_ts:
            self._current.high = max(self._current.high, tick.price)
            self._current.low = min(self._current.low, tick.price)
            self._current.close = tick.price
            self._current.volume += tick.size
        else:
            self._finished.append(self._current)
            self._current = Candle(
                open_ts=bucket, timeframe=self.timeframe, open=tick.price,
                high=tick.price, low=tick.price, close=tick.price, volume=tick.size,
            )

    def current(self) -> Candle | None:
        return self._current

    def finished(self) -> list[Candle]:
        return list(self._finished)


class CoinbaseWSClient:
    """Async Coinbase WebSocket client for BTC-USD.

    Subscribes to the ``ticker`` and ``matches`` channels, feeding the rolling
    price buffer, settlement tracker, and candle builders. All network activity
    is isolated here; the math objects above are independent and reusable.
    """

    def __init__(
        self,
        buffer: RollingPriceBuffer,
        settlement: SettlementTracker,
        candle_1m: CandleBuilder,
        candle_5m: CandleBuilder,
        ws_url: str = "wss://ws-feed.exchange.coinbase.com",
        product_id: str = "BTC-USD",
    ) -> None:
        self.ws_url = ws_url
        self.product_id = product_id
        self.buffer = buffer
        self.settlement = settlement
        self.candle_1m = candle_1m
        self.candle_5m = candle_5m
        self._ws: Any = None
        self._running = False
        self.last_message_ts: float = 0.0

    def _handle_message(self, raw: str | bytes) -> None:
        msg = json.loads(raw if isinstance(raw, str) else raw.decode())
        mtype = msg.get("type")
        self.last_message_ts = time.time()
        if mtype == "ticker":
            price = msg.get("price")
            if price is not None:
                self.buffer.add_price(self.last_message_ts, float(price))
        elif mtype == "match":
            tick = Tick(
                ts=self.last_message_ts,
                price=float(msg.get("price", 0)),
                size=float(msg.get("size", 0)),
                side=str(msg.get("side", "buy")),
            )
            self.buffer.add(tick)
            self.candle_1m.add_tick(tick)
            self.candle_5m.add_tick(tick)

    async def _subscribe(self) -> None:
        sub = {
            "type": "subscribe",
            "product_ids": [self.product_id],
            "channels": ["ticker", "matches"],
        }
        await self._ws.send(json.dumps(sub))

    async def run(self) -> None:
        """Run the websocket loop until stopped, with automatic reconnect."""
        import websockets

        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=20
                ) as ws:
                    self._ws = ws
                    await self._subscribe()
                    async for raw in ws:
                        self._handle_message(raw)
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport error
                logger.warning("Coinbase WS error: %s; reconnecting", exc)
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        import contextlib

        self._running = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    def start_background(self) -> asyncio.Task:
        return asyncio.create_task(self.run())


def build_coinbase_stack() -> tuple[
    RollingPriceBuffer, SettlementTracker, CandleBuilder, CandleBuilder
]:
    """Convenience factory returning the four decoupled math components."""
    buffer = RollingPriceBuffer(window_seconds=60.0)
    settlement = SettlementTracker(settle_seconds=60.0)
    candle_1m = CandleBuilder(timeframe=60)
    candle_5m = CandleBuilder(timeframe=300)
    return buffer, settlement, candle_1m, candle_5m


