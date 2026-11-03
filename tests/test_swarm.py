"""Tests for the 20-agent swarm (dynamic shrinkage, quorum, dispersion, latency)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from btc_edge.brain.swarm import NUM_AGENTS, Swarm


def _feats(**over: float) -> dict:
    f = {
        "micro_velocity_500ms": 0.0,
        "micro_velocity_2s": 0.0,
        "micro_acceleration": 0.0,
        "realized_vol_10s": 0.01,
        "realized_vol_60s": 0.02,
        "strike_distance_zscore": 0.0,
        "orderbook_imbalance": 0.0,
        "spread_cents": 2.0,
        "time_remaining_norm": 0.5,
        "news_sentiment": 0.0,
    }
    f.update(over)
    return f


def test_swarm_latency():
    """1000 consecutive 20-agent evaluations finish fast (p99 < 5ms)."""
    s = Swarm(seed=0)
    feats = _feats()
    for _ in range(100):  # warm-up
        s.consensus(feats, 0.5)
    times = []
    for _ in range(1000):
        t0 = time.perf_counter()
        s.consensus(feats, 0.5)
        times.append(time.perf_counter() - t0)
    p99 = float(np.percentile(times, 99)) * 1000.0
    assert p99 < 5.0, f"p99 consensus latency too high: {p99:.3f}ms"


def test_quorum_gate_rejects_outlier_noise():
    """4 high-outlier agents must not fire when the other 16 are flat."""
    s = Swarm(seed=0)
    feats = _feats()
    # 16 flat agents predict ~sigmoid(bias) near 0.5; nudge 4 to be extreme.
    s._bias[:4] += 8.0  # these 4 now predict ~1.0 regardless of features
    res = s.consensus(feats, market_mid=0.5, min_edge=0.01)
    # The 4 outliers create a wide IQR spread -> dispersion gate blocks.
    assert res["dispersion_iqr"] > 0.08, f"dispersion_iqr={res['dispersion_iqr']:.4f}"
    # A sane entry gate requires BOTH quorum >= 14 AND tight dispersion.
    tight = res["dispersion_iqr"] <= 0.08
    fire = res["quorum"] >= 14 and tight and res["net_edge"] > 0.01
    assert not fire, f"outlier noise must not fire the gate: {res}"


def test_dynamic_shrinkage_override():
    """A 3-sigma micro_acceleration spike overrides market shrinkage at low samples."""
    s = Swarm(seed=0)
    assert int(s.samples.mean()) == 0  # no live samples yet
    calm = s.shrinkage_weight(_feats(micro_acceleration=0.0))
    spike = s.shrinkage_weight(_feats(micro_acceleration=3.0))  # 3 sigma vs threshold 1.0
    # The breakout must de-weight the market substantially vs the calm case.
    assert spike < calm, f"spike shrinkage {spike:.3f} not < calm {calm:.3f}"
    assert spike <= 0.2, f"spike shrinkage should be low, got {spike:.3f}"


def test_consensus_shrinks_toward_market_when_calm():
    """With no information, consensus leans toward the market mid."""
    s = Swarm(seed=0)
    res = s.consensus(_feats(), market_mid=0.72, min_edge=0.01)
    # Strong maturity shrinkage (0.9) pulls consensus close to 0.72.
    assert abs(res["consensus_prob"] - 0.72) < 0.15, res


def test_update_tracks_brier_and_samples():
    s = Swarm(seed=0)
    feats = _feats()
    before = s.brier_ema.copy()
    s.update(feats, outcome=1.0)
    assert int(s.samples.sum()) == NUM_AGENTS
    assert not np.allclose(s.brier_ema, before)  # Brier EMA moved
