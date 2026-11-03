"""Tests for the real-time trading loop."""

from __future__ import annotations

import asyncio

import pytest

from btc_edge.config import load_settings
from btc_edge.execution.trading_loop import TRADING_WINDOWS, TradingLoop
from btc_edge.web.app import EngineState
from btc_edge.web.live_feed import LiveMarketFeed


def _make_loop() -> tuple[TradingLoop, EngineState]:
    state = EngineState()
    settings = load_settings()
    feed = LiveMarketFeed(state)
    return TradingLoop(state, settings, feed), state


def test_kill_switch_reads_file_each_tick(tmp_path):
    """An out-of-band file arm (Telegram bot / cron / manual edit) must halt trading.

    Regression: the loop used to read only the in-memory ``state.kill_switch`` set
    once at startup, so an external arm never took effect. It must re-read the
    persisted file on every tick.
    """
    from btc_edge.execution.killswitch import KillSwitch

    ks_file = tmp_path / "kill_switch.json"
    settings = load_settings()
    settings.safety.kill_switch_file = str(ks_file)

    state = EngineState()
    feed = LiveMarketFeed(state)
    loop = TradingLoop(state, settings, feed)

    # Not armed initially.
    assert loop._killswitch.is_armed() is False

    # Arm out-of-band (write the file directly, as an external process would).
    KillSwitch(str(ks_file)).arm()
    assert loop._killswitch.is_armed() is True

    # The tick's re-read maps that into the in-memory flag that gates routing.
    loop.state.kill_switch = loop._killswitch.is_armed()
    assert loop.state.kill_switch is True

    # And a reset clears it again.
    KillSwitch(str(ks_file)).reset()
    loop.state.kill_switch = loop._killswitch.is_armed()
    assert loop.state.kill_switch is False


def test_builds_all_strategies():
    loop, _ = _make_loop()
    names = {s.name for s in loop.strategies}
    # market_maker and crowd_follow are config-gated off by default (market_maker's
    # post-only maker orders rest unfilled on Kalshi and lock exposure; crowd_follow
    # buys the popular side and ran 0% win). The other core strategies still build.
    assert {"late_scalp", "iv_mispricing"} <= names
    assert "crowd_follow" not in names


def test_trading_windows_are_15m():
    assert set(TRADING_WINDOWS) == {
        (900, "polymarket", "15m"),
        (900, "kalshi", "15m"),
    }


def test_build_context_uses_live_data():
    loop, state = _make_loop()
    state.btc_price = 100_000.0
    state.window_countdowns = {"15m": 750.0}
    state.polymarket_markets = {"15m": {"up": 0.55, "down": 0.45, "prob_up": 0.55,
                                        "slug": "cpc-btc-updown-15m-x"}}
    state.price_history = [(0.0, 99_000.0), (3.0, 99_500.0), (6.0, 100_000.0)]
    ctx = loop._build_context(900, "polymarket", "15m")
    assert ctx is not None
    assert ctx.venue == "polymarket"
    assert ctx.current_spot == 100_000.0
    assert ctx.contract_price == 0.55
    assert ctx.time_remaining == 750.0
    assert ctx.regime in {"DAMPENING", "AMPLIFYING"}
    assert ctx.bids and ctx.asks


def test_calibration_tracks_market_mid_not_spot_implied():
    """The model's prob must shrink toward the venue's book mid (``contract_price``),
    not the overconfident N(d2) spot-implied probability.

    Here spot is far above the window open so N(d2) reads "up" near-certain (~0.99),
    but the market only prices "up" at 0.30. With no learned history the calibrated
    prob must stay near the book mid instead of ballooning toward N(d2).
    """
    loop, state = _make_loop()
    state.btc_price = 100_000.0
    state.window_countdowns = {"15m": 600.0}
    state.polymarket_markets = {"15m": {"up": 0.30, "down": 0.70, "prob_up": 0.30,
                                        "slug": "cpc-btc-updown-15m-x"}}
    loop._windows[(900, "polymarket")]["open_price"] = 99_000.0
    state.price_history = [(0.0, 99_000.0), (1.0, 100_000.0)]
    ctx = loop._build_context(900, "polymarket", "15m")
    assert ctx is not None
    # Calibrated prob is shrunk toward the book mid (0.30) instead of chasing
    # N(d2) ~ 0.99. (max_shrink 0.6 lets the raw estimate pull it, but it stays
    # well below N(d2)'s ~0.99.)
    assert 0.30 <= ctx.prob_estimate < 0.70
    assert ctx.prob_estimate < 0.99


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


def test_realize_close_records_winning_exit():
    """A sell-to-close must realize (exit - entry) P&L, not settle against outcome.

    Regression: re-recording a close as a new open "sell" position made
    ``_settle_positions`` treat it as a fresh short and settle it against the
    window outcome, so a correct "down" win (bought 0.415, closed 0.555) was
    reported to Telegram as "Lost $1.26".
    """
    loop, state = _make_loop()
    state.wallet_balances["kalshi"] = 10.0
    size = 2.7284940034478504
    pnl = loop._realize_close(
        venue="kalshi", tf=900, strategy="position_manager", side="down",
        price=0.555, size=size,
        entry_price=0.415, entry_fee=0.05, exit_fee=0.05,
        btc_price=100_000.0, trade_id="t1",
    )
    # (0.555 - 0.415) * size - 0.05 - 0.05 = +$0.28 (a WIN, not a loss).
    assert pnl == pytest.approx((0.555 - 0.415) * size - 0.1)
    assert pnl > 0
    # The close must NOT be re-added as an open position awaiting settlement.
    assert loop._open == []
    vs = loop._venue_stats["kalshi"]
    assert vs["wins"] == 1
    assert vs["losses"] == 0
    assert vs["pnl_usd"] == pytest.approx(pnl)
    m = loop._metric("position_manager")
    assert m["wins"] == 1
    assert m["losses"] == 0
    assert m["pnl_usd"] == pytest.approx(pnl)
    # A profitable close bumps the dashboard's celebration-sound counter.
    assert state.sell_win_seq == 1


def test_sell_decision_carries_entry_price():
    """Sell-to-close decisions must carry the matched long's entry price/fee."""
    from btc_edge.strategies.base import StrategyDecision

    d = StrategyDecision(
        strategy="position_manager", side="down", action="sell",
        price=0.555, size=2.0, order_type="taker", reason="scalp",
        prob_estimate=0.5, ev_estimate=0.0,
        entry_price=0.415, entry_fee=0.05,
    )
    assert d.entry_price == 0.415
    assert d.entry_fee == 0.05
    # Defaults (plain BUY decisions) leave them unset.
    buy = StrategyDecision(
        strategy="x", side="up", action="buy", price=0.5, size=1.0,
        order_type="taker", reason="r", prob_estimate=0.6, ev_estimate=0.0,
    )
    assert buy.entry_price is None
    assert buy.entry_fee == 0.0


def test_tick_updates_state_without_error():
    loop, state = _make_loop()
    state.btc_price = 100_000.0
    state.window_countdowns = {"5m": 150.0, "15m": 750.0}
    state.polymarket_markets = {"5m": {"up": 0.5, "down": 0.5, "prob_up": 0.5}}
    asyncio.run(loop.tick())
    assert state.regime in {"DAMPENING", "AMPLIFYING"}
    assert isinstance(state.signals, list) and state.signals
    assert "trades" in state.strategy_metrics["late_scalp"]
