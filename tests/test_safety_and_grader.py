"""Tests for the execution router, safety watchdog, nightly grader, ML ensemble,
configuration, and database layer."""

from __future__ import annotations

import time

import pytest

from btc_edge.config import TradingConfig, load_settings
from btc_edge.execution.router import (
    Router,
    kelly_fraction,
    quarter_kelly_size,
)
from btc_edge.execution.safety import SafetyWatchdog
from btc_edge.ml.model import FEATURE_ORDER, ProbabilityEnsemble, feature_vector
from btc_edge.ml.nightly_grader import (
    ACTIVE,
    QUARANTINE,
    NightlyGrader,
    SettlementRecord,
)
from btc_edge.strategies.base import StrategyDecision

# ------------------------------------------------------------------ config

def test_config_defaults_to_live():
    s = load_settings()
    assert s.trading.mode == "live"


def test_live_requires_allow_flag(monkeypatch):
    s = load_settings()
    monkeypatch.delenv("ALLOW_LIVE_TRADING", raising=False)
    assert not s.live_trading_allowed()
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "TRUE")
    assert s.live_trading_allowed()


# ------------------------------------------------------------------- router

def test_kelly_fraction():
    assert kelly_fraction(0.6, 0.5) == pytest.approx((0.6 - 0.5) / 0.5)
    assert kelly_fraction(0.4, 0.5) == 0.0


def test_quarter_kelly_capped():
    # Quarter-Kelly: 0.25 * ((0.6 - 0.5) / 0.5) * 10000 = 500 (under the 10% cap).
    size = quarter_kelly_size(prob=0.6, price=0.5, bankroll=10000.0, cap_pct=10.0)
    assert size == pytest.approx(500.0, rel=1e-6)
    # The hard survival cap: a huge edge is still capped at 5% of bankroll.
    capped = quarter_kelly_size(prob=0.99, price=0.50, bankroll=10000.0, cap_pct=5.0)
    assert capped == pytest.approx(500.0, rel=1e-6)


def test_quarter_kelly_zero_on_no_edge():
    assert quarter_kelly_size(prob=0.4, price=0.5, bankroll=10000.0) == 0.0


def test_trading_config_conservative_defaults():
    """Position sizing defaults must be small and bankroll-aware so a single
    wager can never be a large slice of the account, and total open exposure is
    capped. These are the knobs behind the 'don't burn the bankroll' behavior."""
    cfg = TradingConfig()
    assert cfg.bankroll == 20.0
    assert cfg.quarter_kelly_cap_pct == 15.0
    assert cfg.max_exposure_pct == 35.0
    assert cfg.max_daily_loss_pct == 5.0
    # Polymarket is tuned tighter (more volatile).
    assert cfg.polymarket_cap_pct == 1.0
    assert cfg.polymarket_exposure_pct == 20.0


def test_quarter_kelly_small_cap_pct():
    """With the conservative 2% cap, a $10 bankroll yields at most $0.20 per trade."""
    size = quarter_kelly_size(prob=0.6, price=0.5, bankroll=10.0, cap_pct=2.0)
    # 0.25 * ((0.6 - 0.5) / 0.5) * 10 = 0.5, but capped at 2% * 10 = 0.20.
    assert size == pytest.approx(0.20, rel=1e-6)


def test_router_routes_live():
    s = load_settings()
    router = Router(s)
    decision = StrategyDecision(
        strategy="late_scalp", side="up", action="buy", price=0.9, size=5.0,
        order_type="taker", reason="test", prob_estimate=0.95, ev_estimate=0.04,
    )
    order = router.route(decision, venue="polymarket", outcome=1.0)
    assert order.routed_live  # paper mode removed — positive-size orders route live
    assert order.status == "filled"


def test_size_for_clamps_notional_and_skips_no_edge():
    """`size_for` returns at least the 5-share Polymarket minimum when there is
    positive edge (clamping the notional to the $0.50-$1.15 window), and skips a
    zero/negative-edge order entirely. The total-account-exposure cap is enforced
    separately in the trading loop."""
    s = load_settings()
    router = Router(s)

    d = StrategyDecision(
        strategy="market_maker", side="up", action="buy", price=0.48, size=5.0,
        order_type="taker", reason="test", prob_estimate=0.55, ev_estimate=0.05,
    )
    assert router.size_for(d, venue="polymarket", bankroll=7.0) == 5.0

    # No edge (prob <= price) -> skipped.
    d2 = StrategyDecision(
        strategy="market_maker", side="up", action="buy", price=0.90, size=5.0,
        order_type="taker", reason="test", prob_estimate=0.85, ev_estimate=-0.05,
    )
    assert router.size_for(d2, venue="polymarket", bankroll=7.0) == 0.0


# ------------------------------------------------------------------- safety

def test_safety_kill_switch_and_flush():
    w = SafetyWatchdog(bankroll=10000.0)
    w.trigger_kill_switch()
    assert w.state.kill_switch
    w.emergency_flush()


def test_safety_health_timeout():
    w = SafetyWatchdog(bankroll=10000.0)
    w.heartbeat("coinbase", ts=time.time() - 100.0)
    assert not w.healthy(["coinbase"], now=time.time())


def test_safety_daily_loss_breach():
    w = SafetyWatchdog(bankroll=10000.0, max_daily_loss_pct=5.0)
    w.record_pnl(-600.0)
    assert w.daily_loss_limit_breached()
    assert w.check_and_halt([])
    assert w.state.halted


def test_safety_startup_grace_skips_health_check():
    """During startup grace, a missing heartbeat must NOT fire an emergency flush."""
    w = SafetyWatchdog(bankroll=10000.0)
    # No heartbeat recorded, but we're within the 60s grace -> no halt/flush.
    assert w.check_and_halt(["coinbase"]) is False
    assert not w.state.halted


# ------------------------------------------------------------------- grader

def _records() -> list[SettlementRecord]:
    def _rec(strategy: str, ts: int, ev: float, win: bool) -> SettlementRecord:
        return SettlementRecord(
            strategy=strategy, rule_id="r", window_ts=ts, net_ev=ev,
            win=win, prob=0.8, outcome=1.0 if win else 0.0,
        )

    return [
        _rec("a", 1, 0.1, True),
        _rec("a", 2, -0.05, False),
        _rec("a", 3, 0.2, True),
        _rec("b", 1, -0.1, False),
        _rec("b", 2, -0.1, False),
    ]


def test_grader_computes_metrics():
    g = NightlyGrader(trailing_window=200)
    results = g.grade(_records())
    a = results[("a", "r")]
    b = results[("b", "r")]
    assert a.n == 3
    assert a.net_ev == pytest.approx((0.1 - 0.05 + 0.2) / 3)
    assert a.win_rate == pytest.approx(2 / 3)
    assert a.status == ACTIVE
    assert b.status == QUARANTINE
    assert b.net_ev == pytest.approx(-0.1)


def test_grader_quarantine_gate():
    g = NightlyGrader(trailing_window=200)
    results = g.grade(_records())
    assert g.is_quarantined(results, "b", "r")
    assert not g.is_quarantined(results, "a", "r")


# -------------------------------------------------------------------- ml

def test_feature_vector_shape():
    v = feature_vector({"window_delta_pct": 0.1, "time_remaining": 150.0}, timeframe_seconds=300)
    assert v.shape[0] == len(FEATURE_ORDER)


def test_ensemble_fallback_and_learning():
    ens = ProbabilityEnsemble()
    assert ens.predict({"option_implied_prob": 0.7}) == pytest.approx(0.7)

    for i in range(8):
        ens.update(
            {"window_delta_pct": 0.01 * i, "option_implied_prob": 0.5 + 0.01 * i,
             "book_imbalance": 0.1, "micro_acceleration": 0.0, "time_remaining": 100.0,
             "regime_dampening": 1.0, "regime_amplifying": 0.0, "contract_price": 0.5,
             "realized_vol": 0.2, "implied_vol": 0.45},
            outcome=1.0 if i % 2 == 0 else 0.0,
        )
    ens.retrain_lgbm()
    p = ens.predict(
        {"window_delta_pct": 0.02, "option_implied_prob": 0.6, "book_imbalance": 0.1,
         "micro_acceleration": 0.0, "time_remaining": 100.0, "regime_dampening": 1.0,
         "regime_amplifying": 0.0, "contract_price": 0.55, "realized_vol": 0.2, "implied_vol": 0.45},
    )
    assert 0.0 <= p <= 1.0


def test_calibrate_no_history_defers_to_market():
    ens = ProbabilityEnsemble()
    # No resolved windows yet -> the model is unproven -> max market shrinkage.
    assert ens.shrinkage_weight() == pytest.approx(0.6)
    out = ens.calibrate(0.6, 0.4)
    assert out == pytest.approx(0.4 * 0.6 + 0.6 * 0.4)


def test_shrinkage_weight_rewards_model_skill():
    ens = ProbabilityEnsemble()
    # Seed a resolved history where the model predicts perfectly and the market
    # is uninformative (always 0.5), so the model earns full skill.
    ens._hist_pred = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
    ens._hist_market = [0.5] * 10
    ens._history_y = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    # skill = 1.0, damped by the prior n/(n+20) = 10/30 -> w = max - (1/3)*span.
    w = ens.shrinkage_weight()
    assert w < ens.max_shrink
    assert w == pytest.approx(0.6 - (10.0 / 30.0) * (0.6 - 0.3), abs=0.01)


def test_shrinkage_weight_penalizes_worse_model():
    ens = ProbabilityEnsemble()
    # Model predicts a coin-flip (Brier 0.25); market predicts 0.7/0.3 (Brier
    # ~0.09). The model is clearly worse, so it earns zero skill -> max shrinkage.
    ens._hist_pred = [0.5] * 10
    ens._hist_market = [0.7, 0.3, 0.7, 0.3, 0.7, 0.3, 0.7, 0.3, 0.7, 0.3]
    ens._history_y = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]
    assert ens.shrinkage_weight() == pytest.approx(0.6)


# --------------------------------------------------------------------- db

def test_database_roundtrip(tmp_path):
    from btc_edge.db.models import Signal, Window
    from btc_edge.db.session import Database

    db = Database(tmp_path / "test.db")
    try:
        sess = db.session()
        w = Window(
            venue="kalshi", market_slug="KXBTC15M-1", timeframe_seconds=900,
            window_ts=1000, window_open_price=60000.0, settle_price=60100.0,
            resolved=True, outcome="up", result=1.0,
        )
        sess.add(w)
        sess.commit()

        sig = Signal(
            window_id=w.id, strategy="late_scalp", side="up", contract_price=0.9,
            prob_estimate=0.95, window_delta_pct=0.1, option_implied_prob=0.94,
            book_imbalance=0.2, micro_acceleration=0.0, regime="DAMPENING",
            time_remaining=50.0, rule_snapshot_json="{}", ev_estimate=0.04,
        )
        sess.add(sig)
        sess.commit()

        assert sess.query(Window).count() == 1
        assert sess.query(Signal).count() == 1
        sess.close()
    finally:
        db.close()

