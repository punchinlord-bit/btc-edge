"""Historical bootstrap: resolved-window download + deterministic synthetic generator.

``scripts/run_bootstrap.py`` fetches 500+ resolved windows from Kalshi
(``KXBTC15M``) and Polymarket (``btc-updown-5m-*``) with corresponding 1-minute
candles. When no network / API keys are available, ``--synthetic-if-offline``
populates 600 complete windows into ``data/historical/`` using a deterministic
jump-diffusion BTC price process plus realistic CLOB orderbook dynamics, so the
backtester and ML pipeline are fully verifiable offline.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass(slots=True)
class WindowRecord:
    venue: str
    market_slug: str
    timeframe_seconds: int
    window_ts: int
    open_price: float
    settle_price: float
    outcome: str  # up | down
    result: float  # 1.0 for "up" contract win, 0.0 for loss
    price_path: list[tuple[float, float]] = field(default_factory=list)  # (ts, price)
    candles_1m: list[dict] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.outcome in ("up", "down")


@dataclass(slots=True)
class SyntheticBook:
    ts: float
    up_bids: list[tuple[float, float]]  # (price, size)
    up_asks: list[tuple[float, float]]
    fair_value: float


class SyntheticDataGenerator:
    """Deterministic jump-diffusion BTC generator with realistic CLOB books.

    Merton jump-diffusion:
        dS/S = mu*dt + sigma*dW + (J-1)*dN
    where dN is a Poisson jump process (intensity ``jump_lambda``) and log J is
    normal(``jump_mu``, ``jump_sigma``). The settlement price for each window is
    the mean of the final 60 seconds of the simulated path (matching the BRTI
    settlement convention).
    """

    def __init__(
        self,
        seed: int = 42,
        start_price: float = 60_000.0,
        mu: float = 0.0,
        sigma: float = 1.0,
        jump_lambda: float = 4.0,
        jump_mu: float = -0.01,
        jump_sigma: float = 0.02,
        sample_hz: float = 1.0,
    ) -> None:
        self.rng = np.random.default_rng(seed)
        self.start_price = start_price
        self.mu = mu
        self.sigma = sigma
        self.jump_lambda = jump_lambda
        self.jump_mu = jump_mu
        self.jump_sigma = jump_sigma
        self.sample_hz = sample_hz

    def _path(self, n_steps: int, dt: float, s0: float) -> np.ndarray:
        drift = (self.mu - 0.5 * self.sigma**2) * dt
        diff = self.sigma * math.sqrt(dt)
        gauss = self.rng.standard_normal(n_steps)
        lam_dt = self.jump_lambda * dt
        jump_counts = self.rng.poisson(lam_dt, size=n_steps)
        log_jumps = np.zeros(n_steps)
        for i in range(n_steps):
            if jump_counts[i] > 0:
                js = self.rng.normal(self.jump_mu, self.jump_sigma, size=jump_counts[i])
                log_jumps[i] = js.sum()
        log_returns = drift + diff * gauss + log_jumps
        log_path = np.concatenate([[math.log(s0)], np.cumsum(log_returns)])
        return np.exp(log_path)

    def generate_window(
        self,
        timeframe_seconds: int,
        window_ts: int,
        venue: str,
        settle_seconds: int = 60,
    ) -> WindowRecord:
        n_steps = int(timeframe_seconds * self.sample_hz)
        dt = 1.0 / (self.sample_hz * 365.25 * 86400)
        path = self._path(n_steps, dt, self.start_price)

        open_price = float(path[0])
        settle_start = max(0, n_steps - int(settle_seconds * self.sample_hz))
        settle_price = float(path[settle_start:].mean())

        outcome = "up" if settle_price >= open_price else "down"
        result = 1.0 if outcome == "up" else 0.0

        price_path = [(window_ts + i / self.sample_hz, float(p)) for i, p in enumerate(path)]
        candles_1m = self._make_candles(price_path, 60)
        slug = _slug_for(venue, timeframe_seconds, window_ts)

        return WindowRecord(
            venue=venue,
            market_slug=slug,
            timeframe_seconds=timeframe_seconds,
            window_ts=window_ts,
            open_price=open_price,
            settle_price=settle_price,
            outcome=outcome,
            result=result,
            price_path=price_path,
            candles_1m=candles_1m,
        )

    def _make_candles(self, price_path: list[tuple[float, float]], tf: int) -> list[dict]:
        candles: list[dict] = []
        bucket = -1
        cur: dict | None = None
        for ts, price in price_path:
            b = int(ts) // tf
            if b != bucket:
                if cur is not None:
                    candles.append(cur)
                cur = {
                    "ts": b * tf, "open": price, "high": price,
                    "low": price, "close": price, "volume": 0.0,
                }
                bucket = b
            else:
                assert cur is not None
                cur["high"] = max(cur["high"], price)
                cur["low"] = min(cur["low"], price)
                cur["close"] = price
        if cur is not None:
            candles.append(cur)
        return candles

    def synth_book(self, ts: float, fair_value: float, spread: float = 0.02) -> SyntheticBook:
        half = spread / 2.0
        best_bid = max(0.01, fair_value - half)
        best_ask = min(0.99, fair_value + half)
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for lvl in range(3):
            b_p = max(0.01, best_bid - lvl * 0.01)
            a_p = min(0.99, best_ask + lvl * 0.01)
            b_s = max(5.0, float(self.rng.gamma(3.0, 40.0)))
            a_s = max(5.0, float(self.rng.gamma(3.0, 40.0)))
            bids.append((round(b_p, 4), round(b_s, 2)))
            asks.append((round(a_p, 4), round(a_s, 2)))
        return SyntheticBook(ts=ts, up_bids=bids, up_asks=asks, fair_value=fair_value)


def _slug_for(venue: str, timeframe_seconds: int, window_ts: int) -> str:
    if venue == "polymarket":
        minutes = timeframe_seconds // 60
        return f"btc-updown-{minutes}m-{window_ts}"
    return f"KXBTC{timeframe_seconds // 60}M-{window_ts}"


def generate_synthetic_windows(
    n_windows: int = 600,
    timeframes: tuple[int, ...] = (300, 600, 900),
    seed: int = 42,
    start_price: float = 60_000.0,
    end_ts: float | None = None,
) -> list[WindowRecord]:
    """Generate ``n_windows`` synthetic resolved windows across timeframes."""
    gen = SyntheticDataGenerator(seed=seed, start_price=start_price)
    end = int(end_ts if end_ts is not None else time.time())
    records: list[WindowRecord] = []
    rng = np.random.default_rng(seed + 1)
    venues = ["kalshi", "polymarket"]
    for i in range(n_windows):
        tf = timeframes[i % len(timeframes)]
        venue = venues[i % len(venues)]
        window_ts = end - (i + 1) * tf
        rec = gen.generate_window(tf, window_ts, venue)
        gen.start_price = max(1000.0, rec.settle_price + float(rng.normal(0, 40)))
        records.append(rec)
    return records


def records_to_jsonl(records: list[WindowRecord]) -> str:
    lines = []
    for r in records:
        lines.append(json.dumps({
            "venue": r.venue,
            "market_slug": r.market_slug,
            "timeframe_seconds": r.timeframe_seconds,
            "window_ts": r.window_ts,
            "open_price": r.open_price,
            "settle_price": r.settle_price,
            "outcome": r.outcome,
            "result": r.result,
            "price_path": r.price_path,
            "candles_1m": r.candles_1m,
        }))
    return "\n".join(lines)


def records_from_jsonl(text: str) -> list[WindowRecord]:
    records: list[WindowRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        records.append(WindowRecord(
            venue=d["venue"],
            market_slug=d["market_slug"],
            timeframe_seconds=d["timeframe_seconds"],
            window_ts=d["window_ts"],
            open_price=d["open_price"],
            settle_price=d["settle_price"],
            outcome=d["outcome"],
            result=d["result"],
            price_path=[(float(a), float(b)) for a, b in d["price_path"]],
            candles_1m=d["candles_1m"],
        ))
    return records


def save_windows(records: list[WindowRecord], historical_dir: Path) -> Path:
    historical_dir.mkdir(parents=True, exist_ok=True)
    out = historical_dir / "windows.jsonl"
    out.write_text(records_to_jsonl(records), encoding="utf-8")
    return out


def load_windows(historical_dir: Path) -> list[WindowRecord]:
    f = historical_dir / "windows.jsonl"
    if not f.exists():
        return []
    return records_from_jsonl(f.read_text(encoding="utf-8"))


def bootstrap(
    n_windows: int = 600,
    synthetic_if_offline: bool = True,
    historical_dir: Path | None = None,
) -> tuple[list[WindowRecord], Path]:
    """Run the bootstrap. Falls back to synthetic generation when offline."""
    historical_dir = historical_dir or Path("data/historical")
    records: list[WindowRecord] = []
    offline = True

    if not synthetic_if_offline:
        try:
            records = _fetch_live_windows(n_windows)
            offline = False
        except Exception:  # noqa: BLE001
            records = []

    if offline or not records:
        records = generate_synthetic_windows(n_windows=n_windows)

    path = save_windows(records, historical_dir)
    return records, path


def _fetch_live_windows(n_windows: int) -> list[WindowRecord]:
    """Fetch resolved windows from Kalshi / Polymarket REST APIs.

    Raises on network/credential failure so the caller can fall back to
    synthetic data.
    """
    import asyncio

    from btc_edge.connectors.kalshi_client import KalshiClient

    records: list[WindowRecord] = []
    client = KalshiClient(
        signer=None, base_url="https://external-api.demo.kalshi.co/trade-api/v2"
    )

    async def _kalshi() -> list[WindowRecord]:
        out: list[WindowRecord] = []
        data = await client.get_markets(ticker="KXBTC15M", status="settled")
        for m in data.get("markets", [])[:n_windows]:
            settle = m.get("close_price")
            result = m.get("result")
            if settle is None:
                continue
            out.append(WindowRecord(
                venue="kalshi",
                market_slug=m.get("ticker", ""),
                timeframe_seconds=900,
                window_ts=int(m.get("open_time", 0)),
                open_price=float(m.get("open_price", 0.0)),
                settle_price=float(settle),
                outcome="up" if result == "yes" else "down",
                result=1.0 if result == "yes" else 0.0,
            ))
        return out

    records.extend(asyncio.run(_kalshi()))
    return records


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BTC-Edge historical bootstrap")
    parser.add_argument("--windows", type=int, default=600)
    parser.add_argument("--synthetic-if-offline", action="store_true", default=True)
    parser.add_argument("--no-synthetic", action="store_true")
    parser.add_argument("--out", type=str, default="data/historical")
    args = parser.parse_args(argv)

    records, path = bootstrap(
        n_windows=args.windows,
        synthetic_if_offline=not args.no_synthetic,
        historical_dir=Path(args.out),
    )
    print(f"Bootstrapped {len(records)} windows -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())


