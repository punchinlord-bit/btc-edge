"""Core quantitative indicators.

* ``window_delta_pct`` — distance of the live BRTI proxy from the window open
  reference, weighted 5x-7x higher than standard technical analysis.
* ``option_implied_prob`` — Black-Scholes digital (``N(d2)``) probability that
  the contract settles in-the-money.
* ``book_imbalance`` — top-N CLOB depth imbalance.
* ``micro_acceleration`` — 1-minute candle delta acceleration plus aggressive
  trade delta divergence.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

# Seconds in a year (for annualizing time-to-expiry).
SECONDS_PER_YEAR = 365.25 * 86400.0


def norm_cdf(x: float) -> float:
    """Standard normal CDF via ``erf`` (no external dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def option_implied_prob(
    spot: float,
    strike: float,
    sigma: float,
    time_remaining_seconds: float,
    risk_free: float = 0.0,
) -> float:
    """Digital-call ``N(d2)`` probability that ``spot > strike`` at expiry.

    Args:
        spot: current underlying (BRTI proxy) price.
        strike: the window-open reference price (digital strike).
        sigma: annualized implied volatility (fraction, e.g. 0.45).
        time_remaining_seconds: seconds until settlement.
        risk_free: risk-free rate (default 0 for crypto).
    """
    if spot <= 0 or strike <= 0:
        return 0.0
    t = max(time_remaining_seconds, 0.0) / SECONDS_PER_YEAR
    if t <= 0:
        return 1.0 if spot > strike else 0.0
    if sigma <= 0:
        return 1.0 if spot > strike else 0.0

    d2 = (math.log(spot / strike) + (risk_free - 0.5 * sigma**2) * t) / (sigma * math.sqrt(t))
    return norm_cdf(d2)


def window_delta_pct(current_proxy: float, window_open_price: float) -> float:
    """Percent distance of the live proxy from the window open reference."""
    if window_open_price <= 0:
        return 0.0
    return (current_proxy - window_open_price) / window_open_price * 100.0


def time_weighted_average(
    samples: Iterable[tuple[float, float]],
    lookback_seconds: float,
    now: float,
) -> float | None:
    """Time-weighted average price over the trailing ``lookback_seconds``.

    ``samples`` is an iterable of ``(timestamp_seconds, price)`` pairs. Computes
    the TWAP via trapezoidal integration over the samples that fall inside
    ``[now - lookback_seconds, now]``. Falls back to the most recent price when
    fewer than two samples fall inside the window, or ``None`` when there is no
    usable price data at all.
    """
    cutoff = now - lookback_seconds
    pts = [
        (float(ts), float(p))
        for ts, p in samples
        if p is not None and float(p) > 0 and cutoff <= float(ts) <= now
    ]
    if not pts:
        pts = [(float(ts), float(p)) for ts, p in samples if p is not None and float(p) > 0]
    if not pts:
        return None
    if len(pts) == 1:
        return pts[0][1]

    total_time = 0.0
    weighted = 0.0
    for i in range(len(pts) - 1):
        t0, p0 = pts[i]
        t1, p1 = pts[i + 1]
        dt = t1 - t0
        if dt <= 0:
            continue
        total_time += dt
        weighted += (p0 + p1) / 2.0 * dt
    if total_time <= 0:
        return pts[-1][1]
    return weighted / total_time


def book_imbalance(bid_sizes: Iterable[float], ask_sizes: Iterable[float], levels: int = 3) -> float:
    """Top-N depth imbalance in [-1, 1]."""
    bid_depth = sum(float(s) for s in list(bid_sizes)[:levels])
    ask_depth = sum(float(s) for s in list(ask_sizes)[:levels])
    total = bid_depth + ask_depth
    if total <= 0:
        return 0.0
    return (bid_depth - ask_depth) / total


def micro_acceleration(
    candle_deltas: Iterable[float],
    aggressive_trade_delta_divergence: float = 0.0,
) -> float:
    """1-minute candle delta acceleration plus aggressive-trade divergence.

    ``candle_deltas`` are the per-minute close-to-close returns; acceleration is
    the difference of the two most recent deltas. ``aggressive_trade_delta_divergence``
    is the net buy-vs-sell aggressive volume divergence in the last minute.
    """
    deltas = list(candle_deltas)
    accel = 0.0
    if len(deltas) >= 2:
        accel = deltas[-1] - deltas[-2]
    return accel + aggressive_trade_delta_divergence


def locked_settlement_trajectory(
    locked_average: float | None,
    current_spot: float,
    elapsed_seconds: float,
    total_seconds: float = 60.0,
) -> float:
    """Blend the locked settlement average with the live spot for the remaining
    seconds, producing the expected final settlement price."""
    if locked_average is None:
        return current_spot
    remaining = max(0.0, total_seconds - elapsed_seconds)
    return (locked_average * elapsed_seconds + current_spot * remaining) / total_seconds
