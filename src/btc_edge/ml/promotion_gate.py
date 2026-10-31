"""Champion vs. Challenger promotion gate.

A disciplined self-improvement layer between the self-learning loop and live
capital. The learning loop may never mutate the live ``CHAMPION`` model in place:
every proposed tweak is registered as a ``CHALLENGER`` and must clear five hard
gates against strictly out-of-sample (OOS) 15-minute BTC windows before it is
atomically promoted (and the prior champion archived).

Gates: (1) sample floor, (2) Brier calibration, (3) net-EV edge after fees,
(4) multi-regime win floor, (5) ±10% jitter robustness. Pure stdlib, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

CHAMPION = "CHAMPION"
CHALLENGER = "CHALLENGER"
REJECTED = "REJECTED"
ARCHIVED = "ARCHIVED"

CHOP = "chop"
TREND = "trend"
SPIKE = "spike"
REGIMES: tuple[str, ...] = (CHOP, TREND, SPIKE)


def classify_3regime(abs_move_pct: float) -> str:
    """Map a 15m window's absolute price move (%) to chop / trend / spike.

    Thresholds are tuned for a 15-minute BTC window:
    * ``<= 0.03%`` — effectively flat (chop / mean-reversion).
    * ``>= 0.15%`` — a large directional or liquidation-driven move (spike).
    * otherwise — a moderate directional expansion (trend).
    """
    if abs_move_pct >= 0.15:
        return SPIKE
    if abs_move_pct <= 0.03:
        return CHOP
    return TREND


@dataclass(slots=True)
class WindowEvaluation:
    """One resolved 15-minute BTC window (a single OOS sample for the gate)."""

    window_start_sec: int
    strike_price: float
    close_price: float
    outcome: int  # 1 if "up" won, 0 if "down" won
    regime: str  # chop | trend | spike
    features: dict[str, float] = field(default_factory=dict)
    market_ask_up: float = 0.55
    market_ask_down: float = 0.55
    fee_rate: float = 0.015


@dataclass(slots=True)
class StrategyVersion:
    """A versioned model configuration in the registry."""

    version_id: str
    market_key: str = "btc_15m"
    parent_version_id: str | None = None
    status: str = CHALLENGER
    config: dict[str, float] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    gate_checks: dict[str, Any] = field(default_factory=dict)
    created_by: str = "learning_loop"


@dataclass(slots=True)
class GateResult:
    """The outcome of a head-to-head challenger evaluation."""

    passed: bool
    challenger_version_id: str = ""
    champion_version_id: str = ""
    challenger_brier: float = 0.0
    champion_brier: float = 0.0
    challenger_net_ev: float = 0.0
    champion_net_ev: float = 0.0
    regime_win_rates: dict[str, float] = field(default_factory=dict)
    jitter_retention: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def predict_prob(config: dict[str, float], features: dict[str, float]) -> float:
    """Logistic probability from a weight config.

    ``config`` maps feature names to weights plus an optional ``"bias"`` key.
    ``features`` supplies the per-window feature values (absent -> 0.0).
    """
    z = float(config.get("bias", 0.0))
    for key, weight in config.items():
        if key == "bias":
            continue
        z += weight * float(features.get(key, 0.0))
    try:
        p = 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        return 0.0 if z < 0 else 1.0
    return float(min(0.999, max(0.001, p)))


def _simulate(
    config: dict[str, float], windows: list[WindowEvaluation]
) -> tuple[float, float, dict[str, float], dict[str, int]]:
    """Return ``(brier, net_ev_per_window, {regime: win_rate}, {regime: count})``."""
    n = len(windows)
    if n == 0:
        return 0.0, 0.0, {r: 0.0 for r in REGIMES}, {r: 0 for r in REGIMES}

    brier_sum = 0.0
    ev_sum = 0.0
    regime_wins = {r: 0 for r in REGIMES}
    regime_count = {r: 0 for r in REGIMES}

    for w in windows:
        p_up = predict_prob(config, w.features)
        brier_sum += (p_up - w.outcome) ** 2

        if p_up >= 0.5:
            profit = (w.outcome - w.market_ask_up) - w.fee_rate
        else:
            profit = ((1.0 - w.outcome) - w.market_ask_down) - w.fee_rate
        ev_sum += profit

        if w.regime in regime_count:
            regime_count[w.regime] += 1
            if profit > 0.0:
                regime_wins[w.regime] += 1

    brier = brier_sum / n
    net_ev = ev_sum / n
    win_rates = {
        r: (regime_wins[r] / regime_count[r] if regime_count[r] else 0.0)
        for r in REGIMES
    }
    return brier, net_ev, win_rates, regime_count


def _corner_configs(config: dict[str, float], pct: float) -> list[dict[str, float]]:
    """±``pct`` corner sensitivity matrix (2^N corners for N<=10 params)."""
    keys = [k for k in config if isinstance(config[k], (int, float))]
    if not keys:
        return []

    if len(keys) <= 10:
        corners: list[dict[str, float]] = [dict(config)]
        for k in keys:
            base = float(config[k])
            expanded: list[dict[str, float]] = []
            for c in corners:
                up = dict(c)
                up[k] = base * (1.0 + pct)
                down = dict(c)
                down[k] = base * (1.0 - pct)
                expanded.extend([up, down])
            corners = expanded
        return [c for c in corners if c != config]

    variants: list[dict[str, float]] = []
    for k in keys:
        up = dict(config)
        up[k] = float(config[k]) * (1.0 + pct)
        down = dict(config)
        down[k] = float(config[k]) * (1.0 - pct)
        variants.extend([up, down])
    return variants


class PromotionGate:
    """Evaluates challengers against the champion over OOS windows."""

    def __init__(
        self,
        min_oos_windows: int = 150,
        brier_margin: float = 0.005,
        ev_margin: float = 0.0125,
        regime_floor: float = 0.515,
        jitter_retention: float = 0.75,
        jitter_pct: float = 0.10,
    ) -> None:
        self.min_oos_windows = min_oos_windows
        self.brier_margin = brier_margin
        self.ev_margin = ev_margin
        self.regime_floor = regime_floor
        self.jitter_retention = jitter_retention
        self.jitter_pct = jitter_pct

    def evaluate_and_promote(
        self,
        challenger: StrategyVersion,
        oos_windows: list[WindowEvaluation],
        champion: StrategyVersion | None,
    ) -> GateResult:
        result = GateResult(
            passed=False,
            challenger_version_id=challenger.version_id,
            champion_version_id=champion.version_id if champion else "",
        )
        checks: dict[str, bool] = {}

        # Gate 1 — sample floor.
        if len(oos_windows) < self.min_oos_windows:
            checks["sample_floor"] = False
            result.reasons.append(
                f"insufficient OOS windows ({len(oos_windows)} < {self.min_oos_windows})"
            )
            result.checks = checks
            return result
        checks["sample_floor"] = True

        c_brier, c_ev, c_regimes, c_counts = _simulate(challenger.config, oos_windows)
        result.challenger_brier = c_brier
        result.challenger_net_ev = c_ev
        result.regime_win_rates = dict(c_regimes)

        if champion is not None:
            k_brier, k_ev, _, _ = _simulate(champion.config, oos_windows)
        else:
            # No champion yet: baseline is a constant 0.5 (always bet up).
            k_brier = sum((0.5 - w.outcome) ** 2 for w in oos_windows) / len(oos_windows)
            k_ev = sum(
                (w.outcome - w.market_ask_up) - w.fee_rate for w in oos_windows
            ) / len(oos_windows)
        result.champion_brier = k_brier
        result.champion_net_ev = k_ev

        checks["brier"] = (k_brier - c_brier) >= self.brier_margin
        if not checks["brier"]:
            result.reasons.append(
                f"Brier did not improve enough ({c_brier:.4f} vs {k_brier:.4f})"
            )

        checks["net_ev"] = (c_ev - k_ev) >= self.ev_margin
        if not checks["net_ev"]:
            result.reasons.append(
                f"net EV edge too small ({c_ev - k_ev:+.4f} < {self.ev_margin:+.4f})"
            )

        active = [r for r in REGIMES if c_counts[r] > 0]
        regime_pass = bool(active) and all(
            c_regimes[r] >= self.regime_floor for r in active
        )
        checks["regime_floor"] = regime_pass
        if not regime_pass:
            worst = min(active, key=lambda r: c_regimes[r]) if active else CHOP
            result.reasons.append(
                f"regime win rate below floor (min {worst}={c_regimes[worst]:.1%} < {self.regime_floor:.1%})"
            )

        jitter_retention = self._jitter_retention(challenger.config, oos_windows, c_ev)
        result.jitter_retention = jitter_retention
        checks["jitter"] = jitter_retention >= self.jitter_retention
        if not checks["jitter"]:
            result.reasons.append(
                f"jitter robustness too low ({jitter_retention:.1%} < {self.jitter_retention:.1%})"
            )

        result.checks = checks
        result.passed = all(checks.values())

        if result.passed:
            if champion is not None:
                champion.status = ARCHIVED
            challenger.status = CHAMPION
            challenger.parent_version_id = champion.version_id if champion else None
            wins = sum(
                1 for w in oos_windows
                if (predict_prob(challenger.config, w.features) >= 0.5) == (w.outcome == 1)
            )
            challenger.metrics = {
                "brier": c_brier,
                "net_ev": c_ev,
                "win_rate": wins / len(oos_windows),
            }
            challenger.gate_checks = dict(checks)
        else:
            challenger.status = REJECTED
        return result

    def _jitter_retention(
        self,
        config: dict[str, float],
        oos_windows: list[WindowEvaluation],
        baseline_ev: float,
    ) -> float:
        if baseline_ev <= 0.0:
            return 0.0
        jittered = _corner_configs(config, self.jitter_pct)
        if not jittered:
            return 0.0
        worst = min(_simulate(cfg, oos_windows)[1] for cfg in jittered)
        return worst / baseline_ev

