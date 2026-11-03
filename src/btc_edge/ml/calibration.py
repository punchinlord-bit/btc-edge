"""Probability calibration + grading tools ported from the V1 ``dune-stocks`` brain.

The V1 15m brain had three techniques the live model was missing:

1. **TWAP-aware probability** — a heavy-tailed (Student-t4) digital probability whose
   variance correctly models the fact that the venue settles on a *60-second average*
   (not an instantaneous print). This aligns the model's probability with the BRTI /
   Chainlink 60s-TWAP oracle instead of a naive ``N(d2)``.
2. **Residual calibration** — an online logistic correction on top of the raw
   probability, learning the model's *systematic* bias (e.g. the over-confidence that
   bought 70-89c contracts at only a 25-39% win rate) via SGD with L2.
3. **Brier skill + decile calibration** — a self-grading scorecard that continuously
   reports how well the model's probabilities match realized outcomes.

All of it is additive and dependency-free (stdlib + ``math``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

# 60-second averaging window used by the BRTI / Chainlink settlement oracle.
AVG_SECONDS = 60.0
# Clamp for the residual correction (logit scale) so a single update can't blow up.
ADJ_CLAMP = 0.35


def t4_cdf(x: float) -> float:
    """Student's-t (4 dof) CDF approximation — heavy-tailed vs the normal CDF.

    Crypto returns are fat-tailed; this gives more mass to far-from-strike
    outcomes than the thin-tailed ``N(d2)``.
    """
    s = x / math.sqrt(1.0 + x * x / 4.0)
    return 0.5 + 0.375 * s * (1.0 - s * s / 12.0)


def sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def logit(p: float) -> float:
    p = min(1.0 - 1e-6, max(1e-6, p))
    return math.log(p / (1.0 - p))


def twap_aware_prob(
    spot: float,
    strike: float,
    tau: float,
    elapsed: float,
    sig_sec: float,
    z_shrink: float = 1.0,
) -> tuple[float, float]:
    """Probability ``spot > strike`` at settlement, aware of the 60s TWAP oracle.

    Args:
        spot: current underlying price.
        strike: the window-open reference price.
        tau: seconds remaining until settlement.
        elapsed: seconds elapsed since the window opened.
        sig_sec: per-second volatility (see :class:`VolTracker`).
        z_shrink: shrink factor on the z-score (1.0 = no shrink).

    Returns ``(prob, z)``. The variance uses ``min(tau, AVG)/3`` because a 60s
    average has 1/3 the variance of an instantaneous print over the overlap, plus a
    correction for the part of the averaging window still in the future.
    """
    if spot <= 0 or strike <= 0 or sig_sec <= 0:
        return 0.5, 0.0
    var = sig_sec * sig_sec * (max(tau - AVG_SECONDS, 0.0) + min(tau, AVG_SECONDS) / 3.0)
    if elapsed < AVG_SECONDS:
        var += sig_sec * sig_sec * (AVG_SECONDS - elapsed) / 3.0
    sd = math.sqrt(max(var, 1e-12))
    z = math.log(spot / strike) / sd
    prob = t4_cdf(z * z_shrink * math.sqrt(2.0))
    return min(0.999, max(0.001, prob)), z


class VolTracker:
    """Fast/slow exponentially-weighted variance -> per-second volatility.

    Mirrors V1 ``Vol``: a fast (30-sample half-life) and slow (360-sample half-life)
    EMA of squared log-returns, blended 0.6 fast / 0.4 slow, then scaled to per-second.
    """

    def __init__(self, fast_hl: int = 30, slow_hl: int = 360, min_samples: int = 10) -> None:
        self.fast_hl = fast_hl
        self.slow_hl = slow_hl
        self.min_samples = min_samples
        self.fast: float | None = None
        self.slow: float | None = None
        self.last: float | None = None
        self.n = 0

    def add_sample(self, px: float) -> None:
        if px <= 0:
            return
        if self.last is not None:
            r2 = math.log(px / self.last) ** 2
            for name, hl in (("fast", self.fast_hl), ("slow", self.slow_hl)):
                lam = 0.5 ** (1.0 / hl)
                v = getattr(self, name)
                setattr(self, name, r2 if v is None else lam * v + (1.0 - lam) * r2)
            self.n += 1
        self.last = px

    def sig_sec(self) -> float | None:
        if self.fast is None or self.slow is None or self.n < self.min_samples:
            return None
        return math.sqrt((0.6 * self.fast + 0.4 * self.slow) / 60.0)


class ResidualCalibrator:
    """Online logistic calibration of a raw probability (ports V1 ``Resid``).

    ``p_adj = sigmoid(logit(p_core) + clamp(b + w·x, ±ADJ_CLAMP))``. The weights
    ``w`` and bias ``b`` are updated by SGD (with L2) so the corrected probability
    converges toward the realized outcome, removing systematic over/under-confidence.
    """

    def __init__(self, n_features: int, lr: float = 0.02, l2: float = 0.02, clamp: float = ADJ_CLAMP) -> None:
        self.w = [0.0] * n_features
        self.b = 0.0
        self.lr = lr
        self.l2 = l2
        self.clamp = clamp
        self.n = 0

    def adj(self, x: Iterable[float]) -> float:
        total = self.b + sum(w * v for w, v in zip(self.w, x, strict=False))
        return max(-self.clamp, min(self.clamp, total))

    def calibrate(self, x: Iterable[float], p_core: float) -> float:
        """Apply the learned correction to ``p_core`` and return the adjusted prob."""
        x = list(x)
        return sigmoid(logit(p_core) + self.adj(x))

    def update(self, x: Iterable[float], p_core: float, y: float) -> None:
        """One SGD step: move weights so ``p_adj`` approaches the outcome ``y``."""
        x = list(x)
        err = sigmoid(logit(p_core) + self.adj(x)) - y
        for i, v in enumerate(x):
            self.w[i] -= self.lr * (err * v + self.l2 * self.w[i])
        self.b -= self.lr * err
        self.n += 1


def brier_score(preds: Iterable[float], outcomes: Iterable[float]) -> float:
    """Mean squared error between predicted prob and 0/1 outcome."""
    pairs = list(zip(preds, outcomes, strict=False))
    if not pairs:
        return 0.0
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def brier_skill(brier: float) -> float:
    """Skill score relative to the 0.25 coinflip baseline (1 = perfect, 0 = coinflip)."""
    return 1.0 - brier / 0.25


def calibration_summary(preds: Iterable[float], outcomes: Iterable[float]) -> dict[str, Any]:
    """Self-grading scorecard: Brier, skill, hit-rate, and a decile calibration curve.

    The calibration curve (``pred`` vs ``freq`` per probability decile) is the key
    diagnostic for over-confidence: if ``pred`` ~0.85 but ``freq`` ~0.5, the model is
    badly miscalibrated and buying those contracts is -EV.
    """
    pairs = [(float(p), float(y)) for p, y in zip(preds, outcomes, strict=False) if 0.0 <= float(p) <= 1.0]
    n = len(pairs)
    if not n:
        return {"n": 0, "brier": None, "skill": None, "hit": None, "calibration": []}

    brier = sum((p - y) ** 2 for p, y in pairs) / n
    hit = sum((p >= 0.5) == (y == 1.0) for p, y in pairs) / n

    calibration: list[dict[str, float]] = []
    for i in range(10):
        lo, hi = i / 10.0, (i + 1) / 10.0 + (1e-9 if i == 9 else 0.0)
        bk = [(p, y) for p, y in pairs if lo <= p < hi]
        if bk:
            calibration.append({
                "bin": i,
                "n": len(bk),
                "pred": sum(p for p, _ in bk) / len(bk),
                "freq": sum(y for _, y in bk) / len(bk),
            })

    return {"n": n, "brier": brier, "skill": brier_skill(brier), "hit": hit, "calibration": calibration}
