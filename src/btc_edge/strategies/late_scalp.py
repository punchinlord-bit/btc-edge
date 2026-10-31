"""Strategy 1: Late-Window Certainty Scalp.

Triggers in the final seconds of a window when the contract is near-certain
(price 0.93-0.98), the window delta has moved >= 0.05% in the contract's
direction, and the regime is DAMPENING. Net EV is computed after fees before
firing; Post-Only maker limit orders are preferred over taker orders.
"""

from __future__ import annotations

from btc_edge.backtest import fee_models
from btc_edge.features.pattern_engine import Condition, Operand, RuleGroup
from btc_edge.strategies.base import MarketContext, Strategy, StrategyDecision


class LateScalpStrategy(Strategy):
    name = "late_scalp"

    def __init__(
        self,
        min_time_remaining: int = 10,
        max_time_remaining: int = 120,
        min_prob: float = 0.93,
        max_prob: float = 0.98,
        min_window_delta_pct: float = 0.05,
        min_ev_threshold: float = 0.01,
    ) -> None:
        self.min_time_remaining = float(min_time_remaining)
        self.max_time_remaining = float(max_time_remaining)
        self.min_prob = min_prob
        self.max_prob = max_prob
        self.min_window_delta_pct = min_window_delta_pct
        self.min_ev_threshold = min_ev_threshold
        self.rule = self._build_rule()

    def _build_rule(self) -> RuleGroup:
        return RuleGroup(
            logical_op="AND",
            conditions=[
                Condition(Operand("time_remaining"), "<=", Operand.literal(self.max_time_remaining)),
                Condition(Operand("time_remaining"), ">=", Operand.literal(self.min_time_remaining)),
                Condition(Operand("contract_price"), ">=", Operand.literal(self.min_prob)),
                Condition(Operand("contract_price"), "<=", Operand.literal(self.max_prob)),
                Condition(Operand("window_delta_pct"), ">=", Operand.literal(self.min_window_delta_pct)),
                Condition(Operand("regime_dampening"), "==", Operand.literal(1.0)),
            ],
        )

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        features = ctx.features()
        snapshot = self.rule.snapshot(features)
        if not snapshot["result"]:
            return []

        side = "up" if ctx.window_delta_pct >= 0 else "down"
        p = ctx.option_implied_prob
        ask = ctx.best_ask
        bid = ctx.best_bid

        # Taker EV after fees.
        taker_fee = fee_models.taker_fee(ctx.venue, 1.0, ask)
        taker_ev = p - ask - taker_fee

        # Maker (post-only at best bid) EV after fees (maker fee = 0).
        maker_ev = p - bid if bid > 0 else -1.0

        decisions: list[StrategyDecision] = []
        if maker_ev > self.min_ev_threshold and bid > 0:
            decisions.append(StrategyDecision(
                strategy=self.name,
                side=side,
                action="buy",
                price=bid,
                size=fee_models.POLYMARKET_MIN_SHARES if ctx.venue == "polymarket" else 1.0,
                order_type="maker",
                reason="post-only maker scalp",
                prob_estimate=p,
                ev_estimate=maker_ev,
                rule_snapshot=snapshot,
            ))
        elif taker_ev > self.min_ev_threshold:
            decisions.append(StrategyDecision(
                strategy=self.name,
                side=side,
                action="buy",
                price=ask,
                size=fee_models.POLYMARKET_MIN_SHARES if ctx.venue == "polymarket" else 1.0,
                order_type="taker",
                reason="net-EV taker scalp",
                prob_estimate=p,
                ev_estimate=taker_ev,
                rule_snapshot=snapshot,
            ))
        return decisions
