"""Tests for regime classifier, indicators, and the deep pattern builder."""

from __future__ import annotations

import pytest

from btc_edge.features.indicators import (
    book_imbalance,
    locked_settlement_trajectory,
    micro_acceleration,
    norm_cdf,
    option_implied_prob,
    window_delta_pct,
)
from btc_edge.features.pattern_engine import (
    Condition,
    Operand,
    RuleGroup,
    parse_rule,
    serialize_rule,
)
from btc_edge.features.regime import (
    Regime,
    RegimeClassifier,
    RegimeInputs,
    realized_vol_from_prices,
)

# ----------------------------------------------------------------- indicators

def test_norm_cdf():
    assert norm_cdf(0.0) == pytest.approx(0.5, abs=1e-9)
    assert norm_cdf(100.0) == pytest.approx(1.0, abs=1e-9)
    assert norm_cdf(-100.0) == pytest.approx(0.0, abs=1e-9)


def test_option_implied_prob_at_the_money():
    # At the money with positive time and vol, digital prob ~ 0.5.
    p = option_implied_prob(spot=100.0, strike=100.0, sigma=0.3, time_remaining_seconds=300)
    assert p == pytest.approx(0.5, abs=0.01)


def test_option_implied_prob_in_the_money():
    p = option_implied_prob(spot=110.0, strike=100.0, sigma=0.3, time_remaining_seconds=300)
    assert p > 0.5
    p_low = option_implied_prob(spot=90.0, strike=100.0, sigma=0.3, time_remaining_seconds=300)
    assert p_low < 0.5


def test_option_implied_prob_expiry_edge():
    assert option_implied_prob(spot=110, strike=100, sigma=0.3, time_remaining_seconds=0) == 1.0
    assert option_implied_prob(spot=90, strike=100, sigma=0.3, time_remaining_seconds=0) == 0.0


def test_window_delta_pct():
    assert window_delta_pct(101.0, 100.0) == pytest.approx(1.0)
    assert window_delta_pct(99.0, 100.0) == pytest.approx(-1.0)
    assert window_delta_pct(100.0, 0.0) == 0.0


def test_book_imbalance():
    assert book_imbalance([100, 100, 100], [100, 100, 100], levels=3) == pytest.approx(0.0)
    assert book_imbalance([300, 0, 0], [100, 0, 0], levels=3) == pytest.approx(0.5)
    assert book_imbalance([], [], levels=3) == 0.0


def test_micro_acceleration():
    assert micro_acceleration([0.001, 0.002, 0.004], 0.0) == pytest.approx(0.002)
    assert micro_acceleration([0.001], 0.0) == 0.0


def test_locked_settlement_trajectory():
    # 30s locked at 100, 30s remaining at 110 -> expected settle ~105.
    est = locked_settlement_trajectory(100.0, 110.0, 30.0, 60.0)
    assert est == pytest.approx(105.0)
    # No locked samples -> current spot.
    assert locked_settlement_trajectory(None, 110.0, 30.0, 60.0) == 110.0


# ------------------------------------------------------------------- regime

def test_realized_vol():
    # Flat prices -> zero vol.
    assert realized_vol_from_prices([100, 100, 100, 100], sample_hz=1.0) == 0.0


def test_regime_dampening_vs_amplifying():
    cls = RegimeClassifier(rv_iv_threshold=1.0, funding_zscore_threshold=2.0)
    assert cls.classify(RegimeInputs(rv_iv_ratio=0.5, funding_zscore=0.0)) == Regime.DAMPENING
    assert cls.classify(RegimeInputs(rv_iv_ratio=1.5)) == Regime.AMPLIFYING
    assert cls.classify(RegimeInputs(rv_iv_ratio=0.5, funding_zscore=3.0)) == Regime.AMPLIFYING
    assert cls.classify(RegimeInputs(rv_iv_ratio=0.5, liquidation_spike=True)) == Regime.AMPLIFYING


# ---------------------------------------------------------- pattern builder

def test_condition_operators():
    f = {"a": 1.0, "b": 2.0}
    assert Condition(Operand("a"), ">", Operand.literal(0.0)).evaluate(f)
    assert Condition(Operand("a"), ">=", Operand.literal(1.0)).evaluate(f)
    assert Condition(Operand("a"), "<", Operand("b")).evaluate(f)
    assert Condition(Operand("a"), "<=", Operand.literal(1.0)).evaluate(f)
    assert Condition(Operand("a"), "==", Operand.literal(1.0)).evaluate(f)
    assert Condition(Operand("a"), "!=", Operand.literal(2.0)).evaluate(f)


def test_operand_math_offsets():
    f = {"x": 2.0}
    # x * 3 + 1 == 7
    op = Operand("x", scale=3.0, offset=1.0)
    assert op.resolve(f) == pytest.approx(7.0)
    # division via scale 1/2, subtraction via negative offset.
    assert Operand("x", scale=0.5, offset=-1.0).resolve(f) == pytest.approx(0.0)


def test_rule_group_and_or():
    f = {"a": 1.0, "b": 0.0, "c": 1.0}
    and_group = RuleGroup("AND", [
        Condition(Operand("a"), ">", Operand.literal(0)),
        Condition(Operand("b"), ">", Operand.literal(0)),
    ])
    assert not and_group.evaluate(f)

    or_group = RuleGroup("OR", [
        Condition(Operand("a"), ">", Operand.literal(0)),
        Condition(Operand("b"), ">", Operand.literal(0)),
    ])
    assert or_group.evaluate(f)


def test_rule_group_nested():
    f = {"a": 1.0, "b": 2.0}
    group = RuleGroup("AND", [
        RuleGroup("OR", [
            Condition(Operand("a"), ">", Operand.literal(0)),
            Condition(Operand("b"), "<", Operand.literal(0)),
        ]),
        Condition(Operand("b"), ">", Operand.literal(1.0)),
    ])
    assert group.evaluate(f)


def test_rule_serialization_roundtrip():
    group = RuleGroup("AND", [
        Condition(Operand("window_delta_pct"), ">", Operand.literal(0.02)),
        RuleGroup("OR", [
            Condition(Operand("book_imbalance"), ">", Operand.literal(0.25)),
            Condition(Operand("micro_acceleration"), ">", Operand.literal(0.0)),
        ]),
    ])
    s = serialize_rule(group)
    parsed = parse_rule(s)
    f = {"window_delta_pct": 0.05, "book_imbalance": 0.1, "micro_acceleration": 0.5}
    assert parsed.evaluate(f) == group.evaluate(f)


def test_invalid_operator_raises():
    with pytest.raises(ValueError):
        Condition(Operand("a"), "??", Operand.literal(1))
    with pytest.raises(ValueError):
        RuleGroup("XOR", [])
