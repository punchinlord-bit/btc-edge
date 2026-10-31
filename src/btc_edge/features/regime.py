"""Higher-timeframe regime classifier.

Classifies market state into:

* ``DAMPENING`` — mean-reverting / pinning: realized vol below implied vol,
  neutral funding, stable open interest.
* ``AMPLIFYING`` — trending / cascade: realized vol above implied vol, extreme
  funding-rate Z-score, or an active liquidation spike.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Regime(StrEnum):
    DAMPENING = "DAMPENING"
    AMPLIFYING = "AMPLIFYING"
    UNKNOWN = "UNKNOWN"


def realized_vol(log_returns: Iterable[float], samples_per_year: float = 365.25 * 86400) -> float:
    """Annualized realized volatility from a series of log returns."""
    rs = list(log_returns)
    if len(rs) < 2:
        return 0.0
    mean = sum(rs) / len(rs)
    var = sum((r - mean) ** 2 for r in rs) / (len(rs) - 1)
    return math.sqrt(var * samples_per_year)


def realized_vol_from_prices(prices: Iterable[float], sample_hz: float = 1.0) -> float:
    """Annualized realized vol from a price series sampled at ``sample_hz`` Hz."""
    ps = [float(p) for p in prices]
    if len(ps) < 2:
        return 0.0
    log_returns = [math.log(ps[i] / ps[i - 1]) for i in range(1, len(ps)) if ps[i - 1] > 0]
    return realized_vol(log_returns, samples_per_year=sample_hz * 365.25 * 86400)


@dataclass(slots=True)
class RegimeInputs:
    rv_iv_ratio: float = 0.0  # realized / implied volatility ratio
    funding_zscore: float = 0.0
    liquidation_spike: bool = False
    oi_stable: bool = True


class RegimeClassifier:
    """Deterministic classifier with configurable thresholds."""

    def __init__(
        self,
        rv_iv_threshold: float = 1.0,
        funding_zscore_threshold: float = 2.0,
    ) -> None:
        self.rv_iv_threshold = rv_iv_threshold
        self.funding_zscore_threshold = funding_zscore_threshold

    def classify(self, inputs: RegimeInputs) -> Regime:
        amplifying = (
            inputs.liquidation_spike
            or abs(inputs.funding_zscore) > self.funding_zscore_threshold
            or inputs.rv_iv_ratio > self.rv_iv_threshold
        )
        if amplifying:
            return Regime.AMPLIFYING
        return Regime.DAMPENING

    def classify_raw(
        self,
        rv_iv_ratio: float,
        funding_zscore: float = 0.0,
        liquidation_spike: bool = False,
    ) -> Regime:
        return self.classify(RegimeInputs(
            rv_iv_ratio=rv_iv_ratio,
            funding_zscore=funding_zscore,
            liquidation_spike=liquidation_spike,
        ))
