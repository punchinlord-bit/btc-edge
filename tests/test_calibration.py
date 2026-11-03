"""Tests for the V1-ported probability calibration + grading tools."""

from __future__ import annotations

import math

from btc_edge.ml.calibration import (
    ResidualCalibrator,
    VolTracker,
    brier_score,
    brier_skill,
    calibration_summary,
    logit,
    sigmoid,
    t4_cdf,
    twap_aware_prob,
)


def test_t4_cdf_is_symmetric_and_heavy_tailed():
    # Symmetry around 0.
    assert abs(t4_cdf(0.0) - 0.5) < 1e-9
    assert abs((t4_cdf(1.5) + t4_cdf(-1.5)) - 1.0) < 1e-9
    # Heavier tails than the normal CDF at the same |x| (more mass far out).
    norm_at_2 = 0.5 * (1.0 + math.erf(2.0 / math.sqrt(2.0)))
    assert t4_cdf(-2.0) > 1.0 - norm_at_2


def test_sigmoid_logit_roundtrip():
    for p in (0.001, 0.1, 0.5, 0.9, 0.999):
        assert abs(sigmoid(logit(p)) - p) < 1e-6


def test_twap_aware_prob_direction_and_uncertainty():
    # Above strike -> >0.5, below strike -> <0.5.
    pu, _ = twap_aware_prob(101.0, 100.0, 600.0, 300.0, 0.0005)
    pd, _ = twap_aware_prob(99.0, 100.0, 600.0, 300.0, 0.0005)
    assert pu > 0.5 > pd
    # Higher vol -> closer to coinflip (more uncertainty).
    pu_hi, _ = twap_aware_prob(101.0, 100.0, 600.0, 300.0, 0.05)
    assert abs(pu_hi - 0.5) < abs(pu - 0.5)
    # Invalid inputs -> coinflip.
    p0, z0 = twap_aware_prob(0.0, 100.0, 600.0, 300.0, 0.0005)
    assert p0 == 0.5 and z0 == 0.0


def test_twap_aware_prob_respects_averaging():
    # A long time remaining (tau large) with a fixed per-second vol yields a more
    # uncertain probability than a short remaining window.
    far, _ = twap_aware_prob(101.0, 100.0, 800.0, 100.0, 0.0005)
    near, _ = twap_aware_prob(101.0, 100.0, 60.0, 840.0, 0.0005)
    assert abs(far - 0.5) < abs(near - 0.5)


def test_vol_tracker_needs_min_samples():
    v = VolTracker(min_samples=3)
    assert v.sig_sec() is None
    for px in (100.0, 100.1, 100.05):
        v.add_sample(px)
    assert v.sig_sec() is None  # only 2 log-return samples yet
    v.add_sample(100.2)
    assert v.sig_sec() is not None
    assert v.sig_sec() > 0.0


def test_residual_calibrator_corrects_overconfidence():
    cal = ResidualCalibrator(n_features=1)
    # The raw model is systematically overconfident: p_core=0.85 but outcome is always 0.
    for _ in range(200):
        cal.update([1.0], 0.85, 0.0)
    # The learned correction must pull the probability DOWN toward the true 0.
    assert cal.calibrate([1.0], 0.85) < 0.85
    # Bias saturates at the clamp; verify it moved meaningfully.
    assert cal.calibrate([1.0], 0.85) <= 0.82


def test_residual_calibrator_no_features_unchanged():
    cal = ResidualCalibrator(n_features=0)
    # With no features, adj = bias only; before any update it's identity.
    assert abs(cal.calibrate([], 0.6) - 0.6) < 1e-9


def test_brier_score_and_skill():
    # Perfect predictions -> Brier 0, skill 1.
    assert brier_score([1.0, 0.0, 1.0], [1.0, 0.0, 1.0]) == 0.0
    assert brier_skill(0.0) == 1.0
    # Coinflip (0.5 always) -> Brier 0.25, skill 0.
    assert abs(brier_score([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) - 0.25) < 1e-9
    assert abs(brier_skill(0.25)) < 1e-9


def test_calibration_summary_detects_overconfidence():
    # Overconfident: predicts ~0.9 but only wins ~60% of the time.
    preds = [0.9] * 10
    outcomes = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0]  # 50% actual
    s = calibration_summary(preds, outcomes)
    assert s["n"] == 10
    assert s["skill"] < 0.0  # worse than coinflip
    # The 0.9 bin shows pred ~0.9 but freq ~0.5 -> miscalibrated.
    bin9 = [c for c in s["calibration"] if c["bin"] == 9][0]
    assert bin9["pred"] > 0.85
    assert bin9["freq"] < 0.65


def test_calibration_summary_empty():
    s = calibration_summary([], [])
    assert s["n"] == 0
    assert s["brier"] is None
    assert s["calibration"] == []
