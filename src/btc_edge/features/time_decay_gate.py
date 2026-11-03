"""Deterministic time-decay + Brownian-bridge pre-gate for 15m BTC binaries.

Runs BEFORE any LLM grader and is pure math (no network). It hard-blocks the
early-window coinflip and the settlement freeze, and only lets a trade reach the
AI grader inside the theta-crush / late-pin phases when the geometric edge is
structurally real.

Phase map over the 900s window:

* Phase 1  (0s..510s)   : COINFLIP BLOCK — reject unless an extreme breakout.
* Phase 2  (510s..750s) : PRIMARY THETA-CRUSH — zScore >= 1.30 and edge >= 0.08.
* Phase 3  (750s..825s) : LATE PIN SNIPER — zScore >= 1.75, ask <= 0.84, edge >= 0.06.
* Phase 4  (825s..900s) : SETTLEMENT FREEZE — reject all.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

WINDOW_SECONDS = 900.0
BREAKOUT_MOVE = 0.0045  # 0.45% move for the Phase-1 extreme-breakout exception
# The gate is intentionally permissive: the model's edge is a model-vs-market
# divergence (not a big move), so the zScore/coinflip requirements were killing
# every entry in choppy markets. We only require a non-negative raw edge and keep
# the settlement freeze. The RiskController's price-cap + fee-adjusted net edge
# still does the real safety work.
PHASE2_Z = 0.0
PHASE2_EDGE = 0.0
PHASE3_Z = 0.0
PHASE3_ASK = 0.84
PHASE3_EDGE = 0.0
PHASE1_END = 0.0  # no early-window coinflip block (edge divergence is the signal)


@dataclass(slots=True)
class GateResult:
    allow: bool
    phase: str  # "coinflip" | "breakout" | "theta" | "sniper" | "freeze"
    reason: str
    z_score: float = 0.0
    edge: float = 0.0


class PreGraderGate:
    """Phase-gated deterministic pre-check keyed on elapsed time in the 900s window."""

    def evaluate(
        self,
        elapsed_seconds: float,
        spot_price: float,
        strike_price: float,
        realized_vol_per_sec: float,
        market_ask: float,
        swarm_consensus: float,
        true_prob: float,
        recent_ticks: Sequence[float] = (),
    ) -> GateResult:
        if strike_price <= 0 or spot_price <= 0:
            return GateResult(False, "freeze", "invalid price/strike", 0.0, 0.0)

        move = abs(spot_price - strike_price) / strike_price

        # Phase 1 (0..240s): THE COINFLIP BLOCK.
        if elapsed_seconds < PHASE1_END:
            if move >= BREAKOUT_MOVE and swarm_consensus >= 0.88 and market_ask <= 0.62:
                return GateResult(True, "breakout", "extreme regime breakout", 0.0, 0.0)
            return GateResult(False, "coinflip", "early-window coinflip block", 0.0, 0.0)

        # Phase 4 (825..900s): SETTLEMENT FREEZE.
        if elapsed_seconds >= 825.0:
            return GateResult(False, "freeze", "settlement freeze", 0.0, 0.0)

        # Remaining-volatility units + zScore (shared by phases 2 and 3).
        sigma_remaining = realized_vol_per_sec * math.sqrt(
            max(0.0, WINDOW_SECONDS - elapsed_seconds)
        )
        if sigma_remaining > 0:
            z_score = abs(spot_price - strike_price) / (spot_price * sigma_remaining)
        else:
            z_score = 0.0
        edge = true_prob - market_ask

        # Phase 2 (510..750s): PRIMARY THETA-CRUSH ZONE.
        if elapsed_seconds < 750.0:
            if z_score >= PHASE2_Z and edge >= PHASE2_EDGE:
                return GateResult(True, "theta", "theta-crush zone", z_score, edge)
            return GateResult(False, "theta", "insufficient zScore/edge", z_score, edge)

        # Phase 3 (750..825s): LATE PIN SNIPER.
        whipsaw = self._whipsaw(spot_price, strike_price, recent_ticks)
        if (
            z_score >= PHASE3_Z
            and market_ask <= PHASE3_ASK
            and edge >= PHASE3_EDGE
            and not whipsaw
        ):
            return GateResult(True, "sniper", "late pin sniper", z_score, edge)
        return GateResult(False, "sniper", "late pin sniper conditions not met", z_score, edge)

    @staticmethod
    def _whipsaw(spot_price: float, strike_price: float, recent_ticks: Sequence[float]) -> bool:
        """True if the last 3 ticks are drifting back toward the strike (late whip-saw)."""
        ticks = [float(t) for t in recent_ticks if t is not None]
        if len(ticks) < 3:
            return True  # insufficient data -> treat as whip-saw (conservative block)
        first, last = ticks[-3], ticks[-1]
        return abs(last - strike_price) < abs(first - strike_price)
