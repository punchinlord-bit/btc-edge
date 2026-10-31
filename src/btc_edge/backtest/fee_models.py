"""Exact fee models for Polymarket and Kalshi, plus slippage and depth caps.

* Polymarket taker fee: ``shares * 0.07 * price * (1 - price)``; maker fee 0.0.
* Kalshi taker fee: ``ceil(0.07 * contracts * price * (1 - price) * 100) / 100``;
  maker fee 0.0.
* Polymarket enforces a 5-share minimum order size.
* Taker orders incur 1 tick of latency slippage; fills are capped at the actual
  top-of-book depth.
"""

from __future__ import annotations

import math

POLYMARKET_TAKER_FEE_SCALE = 0.07
POLYMARKET_MIN_SHARES = 5.0
KALSHI_TAKER_FEE_SCALE = 0.07


def polymarket_taker_fee(shares: float, price: float) -> float:
    return shares * POLYMARKET_TAKER_FEE_SCALE * price * (1.0 - price)


def polymarket_maker_fee(shares: float, price: float) -> float:
    return 0.0


def kalshi_taker_fee(contracts: float, price: float) -> float:
    raw = KALSHI_TAKER_FEE_SCALE * contracts * price * (1.0 - price) * 100
    return math.ceil(raw) / 100.0


def kalshi_maker_fee(contracts: float, price: float) -> float:
    return 0.0


def taker_fee(venue: str, size: float, price: float) -> float:
    """Taker fee for a given venue (``kalshi`` or ``polymarket``)."""
    if venue == "kalshi":
        return kalshi_taker_fee(size, price)
    return polymarket_taker_fee(size, price)


def maker_fee(venue: str, size: float, price: float) -> float:
    return 0.0


def fee(venue: str, size: float, price: float, is_maker: bool) -> float:
    return maker_fee(venue, size, price) if is_maker else taker_fee(venue, size, price)


def apply_min_order(venue: str, size: float) -> float:
    """Enforce the 5-share minimum on Polymarket (returns 0.0 if below min)."""
    if venue == "polymarket" and size < POLYMARKET_MIN_SHARES:
        return 0.0
    return size


def slippage_ticks(price: float, tick_size: float = 0.01) -> float:
    """One tick of adverse slippage for a taker order."""
    return tick_size


def cap_fill_size(size: float, available_depth: float) -> float:
    """Cap a fill at the actual top-of-book depth."""
    return min(size, max(0.0, available_depth))


def effective_fill_price(price: float, is_maker: bool, tick_size: float = 0.01) -> float:
    """Adjust the execution price: takers pay one tick of latency slippage."""
    if is_maker:
        return price
    return price + slippage_ticks(price, tick_size)
