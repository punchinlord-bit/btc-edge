"""Integration test: the trading loop feeds resolved windows into the gate."""

from __future__ import annotations

import time

from btc_edge.config import load_settings
from btc_edge.execution.trading_loop import TradingLoop
from btc_edge.ml.model import ProbabilityEnsemble
from btc_edge.ml.strategy_registry import Registry
from btc_edge.web.app import EngineState
from btc_edge.web.live_feed import LiveMarketFeed


def _make_loop() -> tuple[TradingLoop, Registry]:
    state = EngineState()
    settings = load_settings()
    feed = LiveMarketFeed(state)
    registry = Registry()
    loop = TradingLoop(state, settings, feed, registry=registry)
    return loop, registry


def _features() -> dict[str, float]:
    return {
        "time_remaining": 100.0,
        "realized_vol": 0.3,
        "implied_vol": 0.5,
        "window_delta_pct": 0.05,
        "contract_price": 0.5,
        "option_implied_prob": 0.55,
        "book_imbalance": 0.0,
        "micro_acceleration": 0.0,
        "news_sentiment": 0.0,
        "regime_dampening": 1.0,
        "regime_amplifying": 0.0,
        "current_spot": 90000.0,
        "window_open_price": 90000.0,
    }


def test_record_and_gate_records_window_and_learns():
    loop, registry = _make_loop()
    loop._last_registry_save = time.time()  # skip DB persistence in the test
    ens = ProbabilityEnsemble()

    # 0.111% move -> "trend" regime.
    loop._record_and_gate(ens, 900, "kalshi", _features(), 1.0, 90000.0, 90100.0)

    assert len(registry.windows) == 1
    assert registry.windows[0].outcome == 1
    assert registry.windows[0].regime == "trend"
    assert "window_delta_pct" in registry.windows[0].features
    # First sample has no champion yet -> free online learning.
    assert ens.logreg.fitted is True


def test_record_and_gate_gates_after_champion_exists():
    loop, registry = _make_loop()
    loop._last_registry_save = time.time()
    ens = ProbabilityEnsemble()

    # Fit the ensemble once so champion weights exist before gating.
    ens.update(_features(), 1.0, 900)

    # Seed enough windows so the gate has OOS data (regime floor requires all 3).
    for i in range(10):
        regime = ("chop", "trend", "spike")[i % 3]
        x1 = 1.0 if i % 2 == 0 else -1.0
        from btc_edge.ml.promotion_gate import WindowEvaluation

        registry.record_window(WindowEvaluation(
            window_start_sec=1_700_000_000 + i * 900,
            strike_price=90000.0,
            close_price=90000.0 + x1,
            outcome=1 if x1 > 0 else 0,
            regime=regime,
            features={"bias": 0.0},
        ))

    loop._record_and_gate(ens, 900, "kalshi", _features(), 1.0, 90000.0, 90100.0)

    # A window was still recorded regardless of the gate verdict.
    assert len(registry.windows) == 11
    assert ens.logreg.fitted is True
    # The gate produced a scorecard (sample_floor will be False at only 11 windows,
    # which allows warmup learning — but the scorecard must be populated).
    assert "checks" in registry.last_result
