"""Tests for the live market feed (countdowns, probability normalization, refresh)."""

from __future__ import annotations

import asyncio

from btc_edge.web.app import EngineState
from btc_edge.web.live_feed import LiveMarketFeed, compute_countdowns, to_probability


def test_compute_countdowns_boundaries():
    cd = compute_countdowns(now=0.0)
    assert cd["5m"] == 300.0
    assert cd["15m"] == 900.0
    assert cd["1d"] == 86400.0

    # 150s into the 5m window -> 150s remaining; 15m/1d windows are independent.
    cd2 = compute_countdowns(now=150.0)
    assert cd2["5m"] == 150.0


def test_to_probability_normalizes_cents_and_fractions():
    assert to_probability(0.55) == 0.55
    assert to_probability(55) == 0.55
    assert to_probability("0.6") == 0.6
    assert to_probability("60") == 0.6
    assert to_probability(None) is None
    assert to_probability("") is None


def test_refresh_once_populates_state(monkeypatch):
    state = EngineState()
    feed = LiveMarketFeed(state)

    async def fake_btc() -> float:
        return 100_000.0

    def fake_poly() -> dict:
        return {"5m": {"up": 0.55, "down": 0.45, "prob_up": 0.55, "open_strike": 99_999.0}}

    async def fake_kalshi() -> dict:
        return {"last_price": 0.6, "yes_bid": 0.59, "yes_ask": 0.61, "prob_up": 0.6}

    monkeypatch.setattr(feed, "fetch_btc_price", fake_btc)
    monkeypatch.setattr(feed, "fetch_polymarket", fake_poly)
    monkeypatch.setattr(feed, "fetch_kalshi", fake_kalshi)

    asyncio.run(feed.refresh_once())

    assert state.btc_price == 100_000.0
    assert state.brti_60s == 100_000.0
    assert state.open_strike == 99_999.0
    assert state.polymarket_markets["5m"]["prob_up"] == 0.55
    assert state.kalshi_market["prob_up"] == 0.6
    assert len(state.price_history) == 1
    assert set(state.window_countdowns) == {"5m", "15m", "1d"}
    assert state.feed_updated_at > 0


def test_refresh_once_swallows_fetch_errors(monkeypatch):
    state = EngineState()
    feed = LiveMarketFeed(state)

    async def boom() -> float:
        raise RuntimeError("offline")

    monkeypatch.setattr(feed, "fetch_btc_price", boom)
    monkeypatch.setattr(feed, "fetch_polymarket", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(feed, "fetch_kalshi", boom)

    # Must not raise even when every fetch fails.
    asyncio.run(feed.refresh_once())

    assert state.btc_price is None
    assert set(state.window_countdowns) == {"5m", "15m", "1d"}
