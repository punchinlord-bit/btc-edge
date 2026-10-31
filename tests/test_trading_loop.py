"""Tests for the real-time trading loop."""

from __future__ import annotations

import asyncio

from btc_edge.config import load_settings
from btc_edge.execution.trading_loop import TRADING_WINDOWS, TradingLoop
from btc_edge.web.app import EngineState
from btc_edge.web.live_feed import LiveMarketFeed


def _make_loop() -> tuple[TradingLoop, EngineState]:
    state = EngineState()
    settings = load_settings()
    feed = LiveMarketFeed(state)
    return TradingLoop(state, settings, feed), state


def test_builds_all_strategies():
    loop, _ = _make_loop()
    names = {s.name for s in loop.strategies}
    assert {"late_scalp", "market_maker", "crowd_follow", "iv_mispricing"} <= names


def test_trading_windows_are_5m_and_15m():
    assert set(TRADING_WINDOWS) == {
        (300, "polymarket", "5m"),
        (900, "polymarket", "15m"),
        (86400, "kalshi", "1d"),
    }


def test_build_context_uses_live_data():
    loop, state = _make_loop()
    state.btc_price = 100_000.0
    state.window_countdowns = {"5m": 150.0, "15m": 750.0}
    state.polymarket_markets = {"5m": {"up": 0.55, "down": 0.45, "prob_up": 0.55}}
    state.price_history = [(0.0, 99_000.0), (3.0, 99_500.0), (6.0, 100_000.0)]
    ctx = loop._build_context(300, "polymarket", "5m")
    assert ctx is not None
    assert ctx.venue == "polymarket"
    assert ctx.current_spot == 100_000.0
    assert ctx.contract_price == 0.55
    assert ctx.time_remaining == 150.0
    assert ctx.regime in {"DAMPENING", "AMPLIFYING"}
    assert ctx.bids and ctx.asks


def test_settle_positions_buy_up_win():
    loop, _ = _make_loop()
    loop._open = [{
        "tf": 300, "venue": "polymarket",
        "side": "up", "action": "buy", "price": 0.9, "size": 5.0,
        "fee": 0.0, "strategy": "late_scalp", "prob": 0.95,
    }]
    pnl = loop._settle_positions(300, "polymarket", outcome=1.0)
    assert pnl == (1.0 - 0.9) * 5.0
    assert loop._open == []


def test_settle_positions_buy_down_loss():
    loop, _ = _make_loop()
    loop._open = [{
        "tf": 300, "venue": "polymarket",
        "side": "down", "action": "buy", "price": 0.4, "size": 5.0,
        "fee": 0.0, "strategy": "crowd_follow", "prob": 0.6,
    }]
    # outcome=1 (up) means "down" loses: payoff = 0.0
    pnl = loop._settle_positions(300, "polymarket", outcome=1.0)
    assert pnl == (0.0 - 0.4) * 5.0


def test_tick_updates_state_without_error():
    loop, state = _make_loop()
    state.btc_price = 100_000.0
    state.window_countdowns = {"5m": 150.0, "15m": 750.0}
    state.polymarket_markets = {"5m": {"up": 0.5, "down": 0.5, "prob_up": 0.5}}
    asyncio.run(loop.tick())
    assert state.regime in {"DAMPENING", "AMPLIFYING"}
    assert isinstance(state.signals, list) and state.signals
    assert "trades" in state.strategy_metrics["late_scalp"]
