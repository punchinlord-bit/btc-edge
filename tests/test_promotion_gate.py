"""Tests for the Champion vs. Challenger promotion gate."""

from __future__ import annotations

from btc_edge.ml.promotion_gate import (
    ARCHIVED,
    CHAMPION,
    REJECTED,
    PromotionGate,
    StrategyVersion,
    WindowEvaluation,
    predict_prob,
)


def _win(ts: int, regime: str, outcome: int, x1: float) -> WindowEvaluation:
    """Build a window with a single ``x1`` feature and the given outcome."""
    strike = 90000.0
    return WindowEvaluation(
        window_start_sec=ts,
        strike_price=strike,
        close_price=strike + (1.0 if outcome else -1.0),
        outcome=outcome,
        regime=regime,
        features={"x1": x1},
        market_ask_up=0.55,
        market_ask_down=0.55,
        fee_rate=0.015,
    )


def _no_skill() -> StrategyVersion:
    return StrategyVersion(version_id="v1.0.0-champ", status=CHAMPION, config={"bias": 0.0, "x1": 0.0})


def test_predict_prob_is_logistic():
    assert predict_prob({"bias": 0.0, "x1": 1.0}, {"x1": 0.0}) == 0.5
    assert predict_prob({"bias": 0.0, "x1": 1.0}, {"x1": 1.0}) > 0.5
    assert predict_prob({"bias": 0.0, "x1": 1.0}, {"x1": -1.0}) < 0.5
    assert predict_prob({"bias": 0.0, "x1": 5.0}, {"x1": 10.0}) > 0.99
    assert predict_prob({"bias": 0.0, "x1": 5.0}, {"x1": -10.0}) < 0.01


def test_promotes_genuinely_superior_challenger():
    gate = PromotionGate(min_oos_windows=20)
    champion = _no_skill()
    challenger = StrategyVersion(version_id="v2.0.0", config={"bias": 0.0, "x1": 3.0})

    windows: list[WindowEvaluation] = []
    ts = 1_700_000_000
    for regime in ("chop", "trend", "spike"):
        for i in range(20):
            x1 = 1.0 if i % 2 == 0 else -1.0
            windows.append(_win(ts, regime, 1 if x1 > 0 else 0, x1))
            ts += 900

    result = gate.evaluate_and_promote(challenger, windows, champion)

    assert result.passed is True
    assert challenger.status == CHAMPION
    assert champion.status == ARCHIVED
    assert result.challenger_brier < result.champion_brier
    assert result.challenger_net_ev > result.champion_net_ev
    assert all(r >= 0.515 for r in result.regime_win_rates.values())
    assert result.jitter_retention >= 0.75


def test_rejects_overfit_challenger_on_jitter():
    gate = PromotionGate(min_oos_windows=20)
    champion = _no_skill()
    # Knife-edge threshold: tiny feature perturbations flip predictions.
    challenger = StrategyVersion(version_id="v2.0.0-overfit", config={"bias": -1.0, "x1": 1.0})

    windows: list[WindowEvaluation] = []
    ts = 1_700_000_000
    for regime in ("chop", "trend", "spike"):
        for i in range(20):
            x1 = 1.01 if i % 2 == 0 else 0.99
            windows.append(_win(ts, regime, 1 if x1 >= 1.0 else 0, x1))
            ts += 900

    result = gate.evaluate_and_promote(challenger, windows, champion)

    assert result.passed is False
    assert result.checks["jitter"] is False
    assert challenger.status == REJECTED


def test_rejects_challenger_below_regime_floor():
    gate = PromotionGate(min_oos_windows=20)
    champion = _no_skill()
    challenger = StrategyVersion(version_id="v2.0.0-regime", config={"bias": 0.0, "x1": 5.0})

    windows: list[WindowEvaluation] = []
    ts = 1_700_000_000
    # Trend: aligned signal -> challenger wins.
    for i in range(40):
        x1 = 1.0 if i % 2 == 0 else -1.0
        windows.append(_win(ts, "trend", 1 if x1 > 0 else 0, x1))
        ts += 900
    # Chop + spike: inverted signal -> challenger bets up but loses.
    for regime in ("chop", "spike"):
        for _ in range(10):
            windows.append(_win(ts, regime, 0, 1.0))
            ts += 900

    result = gate.evaluate_and_promote(challenger, windows, champion)

    assert result.passed is False
    assert result.checks["regime_floor"] is False
    assert challenger.status == REJECTED
    # High overall win rate does not save it.
    assert result.regime_win_rates["trend"] > 0.9
    assert result.regime_win_rates["chop"] < 0.515


def test_rejects_when_brier_degrades_despite_positive_pnl():
    gate = PromotionGate(min_oos_windows=20)
    champion = _no_skill()
    # Overconfident "down" bettor: lucky (60% down) but poorly calibrated.
    challenger = StrategyVersion(version_id="v2.0.0-overconfident", config={"bias": -2.2})

    windows: list[WindowEvaluation] = []
    ts = 1_700_000_000
    for regime in ("chop", "trend", "spike"):
        for i in range(20):
            outcome = 0 if i % 5 < 3 else 1  # 60% down, 40% up
            windows.append(_win(ts, regime, outcome, 0.0))
            ts += 900

    result = gate.evaluate_and_promote(challenger, windows, champion)

    assert result.passed is False
    assert result.checks["brier"] is False
    # Net EV is positive (lucky PnL) but calibration is worse than champion.
    assert result.challenger_net_ev > result.champion_net_ev
    assert result.challenger_brier > result.champion_brier
    assert challenger.status == REJECTED


def test_rejects_insufficient_sample():
    gate = PromotionGate(min_oos_windows=150)
    champion = _no_skill()
    challenger = StrategyVersion(version_id="v2.0.0", config={"bias": 0.0, "x1": 3.0})
    windows = [_win(1_700_000_000 + i * 900, "trend", 1, 1.0) for i in range(10)]

    result = gate.evaluate_and_promote(challenger, windows, champion)

    assert result.passed is False
    assert result.checks.get("sample_floor") is False
