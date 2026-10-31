"""Strategy 3: Final 5-Minute Confluence Trend.

Triggers when 180s <= time_remaining <= 300s, market price crosses >= $0.60, and
the Pattern Builder confirms window_delta_pct > 0.02% AND book_imbalance > 0.25
AND micro_acceleration > 0.
"""

from __future__ import annotations

from btc_edge.features.pattern_engine import Condition, Operand, RuleGroup
from btc_edge.strategies.base import MarketContext, Strategy, StrategyDecision


class CrowdFollowStrategy(Strategy):
    name = "crowd_follow"

    def __init__(
        self,
        min_time_remaining: int = 180,
        max_time_remaining: int = 300,
        min_contract_price: float = 0.60,
    ) -> None:
        self.min_time_remaining = float(min_time_remaining)
        self.max_time_remaining = float(max_time_remaining)
        self.min_contract_price = min_contract_price
        self.rule = self._build_rule()

    def _build_rule(self) -> RuleGroup:
        return RuleGroup(
            logical_op="AND",
            conditions=[
                Condition(Operand("time_remaining"), ">=", Operand.literal(self.min_time_remaining)),
                Condition(Operand("time_remaining"), "<=", Operand.literal(self.max_time_remaining)),
                Condition(Operand("contract_price"), ">=", Operand.literal(self.min_contract_price)),
                Condition(Operand("window_delta_pct"), ">", Operand.literal(0.02)),
                Condition(Operand("book_imbalance"), ">", Operand.literal(0.25)),
                Condition(Operand("micro_acceleration"), ">", Operand.literal(0.0)),
            ],
        )

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        snapshot = self.rule.snapshot(ctx.features())
        if not snapshot["result"]:
            return []

        side = "up" if ctx.window_delta_pct > 0 else "down"
        return [StrategyDecision(
            strategy=self.name,
            side=side,
            action="buy",
            price=ctx.best_ask,
            size=5.0,
            order_type="taker",
            reason="confluence trend entry",
            prob_estimate=ctx.prob_estimate,
            ev_estimate=ctx.prob_estimate - ctx.best_ask,
            rule_snapshot=snapshot,
        )]
