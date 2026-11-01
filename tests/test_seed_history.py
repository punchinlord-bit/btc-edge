"""Tests for the historical seeding pipeline (settled Kalshi markets -> samples)."""

from __future__ import annotations

import pytest

from btc_edge.ml.seed_history import markets_to_samples, settled_market_to_sample


def test_settled_market_to_sample_reconstructs_move_and_outcome():
    market = {
        "result": "yes",
        "floor_strike": 77507.3,
        "expiration_value": "77545.13",
    }
    features, outcome = settled_market_to_sample(market)
    assert outcome == 1.0
    # (77545.13 - 77507.3) / 77507.3 * 100 ~= +0.0488%
    assert features["window_delta_pct"] == pytest.approx(0.0488, rel=0.01)
    assert features["option_implied_prob"] == 0.5
    assert features["contract_price"] == 0.5


def test_settled_market_to_sample_down_outcome():
    market = {"result": "no", "floor_strike": 77507.3, "expiration_value": "77481.57"}
    features, outcome = settled_market_to_sample(market)
    assert outcome == 0.0
    assert features["window_delta_pct"] < 0.0


def test_settled_market_to_sample_rejects_invalid():
    # Missing result / strike / expiry must be skipped, not crash.
    assert settled_market_to_sample({}) is None
    assert settled_market_to_sample({"result": "yes", "floor_strike": 0.0, "expiration_value": "1.0"}) is None
    assert settled_market_to_sample({"result": "yes", "floor_strike": 1.0, "expiration_value": 0.0}) is None


def test_markets_to_samples_filters_invalid():
    markets = [
        {"result": "yes", "floor_strike": 77507.3, "expiration_value": "77545.13"},
        {"result": "no", "floor_strike": 77507.3, "expiration_value": "77481.57"},
        {},  # invalid, skipped
    ]
    samples = markets_to_samples(markets)
    assert len(samples) == 2
    assert samples[0][1] == 1.0
    assert samples[1][1] == 0.0
