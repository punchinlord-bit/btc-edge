"""Tests for strategies and the fee-aware backtester."""

from __future__ import annotations

import pytest

from btc_edge.backtest.simulator import Simulator, build_context
from btc_edge.historical.bootstrapper import generate_synthetic_windows
from btc_edge.strategies.base import MarketContext
from btc_edge.strategies.crowd_follow import CrowdFollowStrategy
from btc_edge.strategies.iv_mispricing import IvMispricingStrategy
from btc_edge.strategies.late_scalp import LateScalpStrategy
from btc_edge.strategies.market_maker import MarketMakerStrategy


def make_context(**overrides) -> MarketContext:
    defaults = dict(
        venue="polymarket",
        timeframe_seconds=300,
        window_ts=1000,
        window_open_price=60000.0,
        current_spot=60000.0,
        time_remaining=100.0,
        contract_price=0.95,
        prob_estimate=0.95,
        window_delta_pct=0.05,
        option_implied_prob=0.96,
        book_imbalance=0.0,
        micro_acceleration=0.0,
        regime="DAMPENING",
        bids=[(0.94, 100.0), (0.93, 50.0)],
        asks=[(0.96, 100.0), (0.97, 50.0)],
        realized_vol=0.2,
        implied_vol=0.45,
    )
    defaults.update(overrides)
    return MarketContext(**defaults)


# ------------------------------------------------------------- late scalp

def test_late_scalp_triggers_in_window():
    strat = LateScalpStrategy()
    ctx = make_context(time_remaining=50.0, contract_price=0.95, window_delta_pct=0.10)
    decisions = strat.evaluate(ctx)
    assert len(decisions) >= 1
    assert decisions[0].order_type in ("maker", "taker")


def test_late_scalp_ignores_outside_window():
    strat = LateScalpStrategy()
    ctx = make_context(time_remaining=500.0)
    assert strat.evaluate(ctx) == []


def test_late_scalp_requires_dampening():
    strat = LateScalpStrategy()
    ctx = make_context(time_remaining=50.0, regime="AMPLIFYING")
    assert strat.evaluate(ctx) == []


# ------------------------------------------------------------ market maker

def test_market_maker_quotes_three_levels():
    strat = MarketMakerStrategy()
    ctx = make_context(time_remaining=200.0, prob_estimate=0.50, regime="DAMPENING")
    decisions = strat.evaluate(ctx)
    assert len(decisions) == 6  # 3 bids + 3 asks
    assert all(d.order_type == "maker" for d in decisions)


def test_market_maker_inactive_early():
    strat = MarketMakerStrategy()
    ctx = make_context(time_remaining=50.0)  # below min_time_remaining
    assert strat.evaluate(ctx) == []


# ------------------------------------------------------------ crowd follow

def test_crowd_follow_confluence():
    strat = CrowdFollowStrategy()
    ctx = make_context(
        time_remaining=240.0, contract_price=0.65, window_delta_pct=0.05,
        book_imbalance=0.4, micro_acceleration=0.01,
    )
    decisions = strat.evaluate(ctx)
    assert len(decisions) == 1
    assert decisions[0].side == "up"


def test_crowd_follow_no_confluence():
    strat = CrowdFollowStrategy()
    ctx = make_context(time_remaining=240.0, contract_price=0.65, window_delta_pct=0.0)
    assert strat.evaluate(ctx) == []


# ---------------------------------------------------------- iv mispricing

def test_iv_mispricing_underpriced():
    strat = IvMispricingStrategy(min_divergence=0.045)
    ctx = make_context(
        contract_price=0.50, option_implied_prob=0.60,
        book_imbalance=0.1, micro_acceleration=0.1,
    )
    decisions = strat.evaluate(ctx)
    assert any(d.side == "up" and d.action == "buy" for d in decisions)


def test_iv_mispricing_no_divergence():
    strat = IvMispricingStrategy(min_divergence=0.045)
    ctx = make_context(contract_price=0.50, option_implied_prob=0.51)
    assert strat.evaluate(ctx) == []


# ------------------------------------------------------------- backtester

def test_backtest_end_to_end():
    windows = generate_synthetic_windows(n_windows=30, timeframes=(300, 600, 900), seed=1)
    strategies = [
        LateScalpStrategy(),
        MarketMakerStrategy(),
        CrowdFollowStrategy(),
        IvMispricingStrategy(),
    ]
    sim = Simulator(strategies=strategies, implied_vol=0.45, seed=3)
    stats = sim.run(windows)

    assert set(stats.keys()) == {"late_scalp", "market_maker", "crowd_follow", "iv_mispricing"}
    for s in stats.values():
        assert s.n_fills >= 0
        assert s.brier_score >= 0.0 if s.brier_count > 0 else True
        # win rate bounded
        assert 0.0 <= s.win_rate <= 1.0


def test_backtest_settles_payoff_correctly():
    # A single "up" buy that wins must produce positive gross PnL.
    from btc_edge.backtest.simulator import StrategyStats, _settle_payoff

    assert _settle_payoff("up", 1.0, 10.0) == 10.0
    assert _settle_payoff("up", 0.0, 10.0) == 0.0
    assert _settle_payoff("down", 1.0, 10.0) == 0.0
    assert _settle_payoff("down", 0.0, 10.0) == 10.0

    st = StrategyStats(strategy="x")
    st.record(pnl=1.0, fee=0.05, won=True, prob=0.9, outcome=1.0)
    assert st.gross_pnl == 1.0
    assert st.net_pnl == pytest.approx(0.95)
    assert st.wins == 1


def test_build_context_features():
    windows = generate_synthetic_windows(n_windows=1, timeframes=(300,), seed=5)
    rec = windows[0]
    ctx = build_context(rec, time_remaining=120.0, implied_vol=0.45, market_noise=0.0)
    assert 0.0 <= ctx.contract_price <= 1.0
    assert 0.0 <= ctx.option_implied_prob <= 1.0
    assert ctx.time_remaining == 120.0
    assert len(ctx.bids) == 3 and len(ctx.asks) == 3
