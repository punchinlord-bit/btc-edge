"""Verification suite for the unified risk gate + shadow paper-trading engine.

Mirrors the three production cases:
1. Polymarket high-price (>0.60) trap is blocked (its ceiling is tighter than Kalshi's).
2. Polymarket $0 balance flips to SHADOW mode and grades a Brier/PnL outcome.
3. Kalshi low-edge churn is blocked while the model still retrains via Hedge.
"""

from __future__ import annotations

import pytest

from btc_edge.execution.risk_gate import RiskController
from btc_edge.ml.shadow_ledger import Brain, ShadowLedger


@pytest.fixture
def ledger(tmp_path):
    return ShadowLedger(str(tmp_path / "brain_state.json"))


def _risk():
    return RiskController(
        max_contract_price=0.72, min_net_edge=0.045, shadow_virtual_bankroll=500.0
    )


def test_default_min_net_edge_is_4_5_percent():
    """Regression: the default was accidentally 0.005 (0.5%), which let the ~1.75%
    Kalshi taker fee eat the edge (the account lost $251 over 168 trades)."""
    assert RiskController().min_net_edge == 0.045


def test_thin_edge_blocked_by_default_gate():
    """A ~1.25% net edge must be filtered by the DEFAULT (4.5%) gate."""
    risk = RiskController()
    decision = risk.evaluate_signal(
        venue="kalshi", model_prob_yes=0.56,
        yes_ask=0.52, yes_bid=0.50, no_ask=0.50, no_bid=0.48,
        live_balance=100.0,
    )
    assert decision.approved is False
    assert "INSUFFICIENT_NET_EDGE" in decision.reason


def test_certainty_all_in_sizes_up():
    """Near-certain (>=93c, >=97% confident) bypasses the price cap and sizes up."""
    risk = RiskController(certainty_all_in_pct=0.50)
    decision = risk.evaluate_signal(
        venue="kalshi", model_prob_yes=0.97,
        yes_ask=0.95, yes_bid=0.93, no_ask=0.07, no_bid=0.05,
        live_balance=100.0,
    )
    assert decision.approved is True
    assert decision.mode == "LIVE"
    # ~50% of $100 = $50 (not the conservative 3% Kelly cap).
    assert decision.cost_dollars >= 45.0


def test_high_price_not_certainty_still_blocked():
    """A >0.72 entry WITHOUT >=97% confidence is still blocked by the price cap."""
    risk = RiskController()
    decision = risk.evaluate_signal(
        venue="kalshi", model_prob_yes=0.85,
        yes_ask=0.95, yes_bid=0.93, no_ask=0.07, no_bid=0.05,
        live_balance=100.0,
    )
    assert decision.approved is False
    assert "PRICE_CAP_EXCEEDED" in decision.reason


def test_polymarket_high_price_trap_blocked(ledger):
    decision = _risk().evaluate_signal(
        venue="polymarket", model_prob_yes=0.88,
        yes_ask=0.88, yes_bid=0.86, no_ask=0.14, no_bid=0.12,
        live_balance=250.0,
    )
    assert decision.approved is False
    assert "PRICE_CAP_EXCEEDED" in decision.reason


def test_polymarket_tighter_price_ceiling_blocks_70c():
    """Polymarket must refuse a 70c first bid — its ceiling is 60c, not Kalshi's 72c."""
    decision = _risk().evaluate_signal(
        venue="polymarket", model_prob_yes=0.88,
        yes_ask=0.70, yes_bid=0.68, no_ask=0.32, no_bid=0.30,
        live_balance=250.0,
    )
    assert decision.approved is False
    assert "PRICE_CAP_EXCEEDED" in decision.reason
    assert "0.60" in decision.reason  # the venue-specific ceiling, not 0.72


def test_polymarket_buys_cheap_under_60c():
    """A cheap Polymarket entry (<=60c) with real edge is still allowed."""
    decision = _risk().evaluate_signal(
        venue="polymarket", model_prob_yes=0.72,
        yes_ask=0.58, yes_bid=0.57, no_ask=0.43, no_bid=0.42,
        live_balance=250.0,
    )
    assert decision.approved is True


def test_kalshi_keeps_72c_ceiling():
    """Kalshi is NOT subject to Polymarket's 60c cap; 70c is still tradeable there."""
    decision = _risk().evaluate_signal(
        venue="kalshi", model_prob_yes=0.85,
        yes_ask=0.70, yes_bid=0.68, no_ask=0.32, no_bid=0.30,
        live_balance=250.0,
    )
    assert decision.approved is True


def test_polymarket_zero_balance_shadow_mode_and_grading(ledger):
    brain = Brain(ledger)
    decision = _risk().evaluate_signal(
        venue="polymarket", model_prob_yes=0.72,
        yes_ask=0.58, yes_bid=0.57, no_ask=0.43, no_bid=0.42,
        live_balance=0.0,  # $0 Polymarket wallet
    )
    assert decision.approved is True
    assert decision.mode == "SHADOW"
    assert decision.contracts > 0

    brain.record_decision(
        "BTC-15M-ZERO-BAL", "polymarket",
        {"momentumAgent": 0.74, "orderflowAgent": 0.70, "volatilityAgent": 0.65},
        decision, model_prob_yes=0.72,
    )
    settled = brain.settle("BTC-15M-ZERO-BAL", 1)  # YES wins
    assert settled[0]["won"] is True
    assert settled[0]["pnl"] > 0
    assert ledger.state["metrics"]["polymarket"]["shadowPnL"] > 0
    assert ledger.state["metrics"]["polymarket"]["livePnL"] == 0


def test_kalshi_low_edge_churn_blocked_and_retrains(ledger):
    brain = Brain(ledger)
    initial_weight = ledger.state["modelWeights"]["momentumAgent"]

    decision = _risk().evaluate_signal(
        venue="kalshi", model_prob_yes=0.53,
        yes_ask=0.51, yes_bid=0.49, no_ask=0.51, no_bid=0.49,
        live_balance=100.0,
    )
    assert decision.approved is False
    assert "INSUFFICIENT_NET_EDGE" in decision.reason

    # Even a filtered trade is settled so the brain learns (momentum predicted 0.60
    # on a YES outcome -> should gain weight).
    brain.record_decision(
        "BTC-15M-KALSHI-CHURN", "kalshi",
        {"momentumAgent": 0.60, "orderflowAgent": 0.50, "volatilityAgent": 0.48},
        decision, model_prob_yes=0.53,
    )
    brain.settle("BTC-15M-KALSHI-CHURN", 1)
    updated_weight = ledger.state["modelWeights"]["momentumAgent"]
    assert updated_weight > initial_weight
