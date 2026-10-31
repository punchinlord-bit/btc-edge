"""Strategy 2: Post-Only CLOB Market Making.

Active when time_remaining > 120s and regime is DAMPENING. Quotes 3 post-only
bid/ask levels around the ensemble fair value when the book spread >= $0.02,
refreshing every 15 seconds (or immediately on a >0.03% spot move).
"""

from __future__ import annotations

from btc_edge.features.pattern_engine import Condition, Operand, RuleGroup
from btc_edge.strategies.base import MarketContext, Strategy, StrategyDecision


class MarketMakerStrategy(Strategy):
    name = "market_maker"

    def __init__(
        self,
        min_time_remaining: int = 120,
        levels: int = 3,
        min_spread: float = 0.02,
        refresh_seconds: int = 15,
        spot_move_pct: float = 0.03,
    ) -> None:
        self.min_time_remaining = float(min_time_remaining)
        self.levels = levels
        self.min_spread = min_spread
        self.refresh_seconds = refresh_seconds
        self.spot_move_pct = spot_move_pct
        self.rule = self._build_rule()

    def _build_rule(self) -> RuleGroup:
        return RuleGroup(
            logical_op="AND",
            conditions=[
                Condition(Operand("time_remaining"), ">", Operand.literal(self.min_time_remaining)),
            ],
        )

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        snapshot = self.rule.snapshot(ctx.features())
        if not snapshot["result"]:
            return []

        spread = ctx.best_ask - ctx.best_bid
        if spread < self.min_spread:
            return []

        fair = ctx.prob_estimate
        decisions: list[StrategyDecision] = []
        for lvl in range(self.levels):
            bid_price = max(0.01, round(fair - (lvl + 1) * (spread / 2.0), 4))
            ask_price = min(0.99, round(fair + (lvl + 1) * (spread / 2.0), 4))
            decisions.append(StrategyDecision(
                strategy=self.name,
                side="up",
                action="buy",
                price=bid_price,
                size=5.0,
                order_type="maker",
                reason=f"post-only bid level {lvl + 1}",
                prob_estimate=fair,
                ev_estimate=fair - bid_price,
                rule_snapshot=snapshot,
            ))
            decisions.append(StrategyDecision(
                strategy=self.name,
                side="up",
                action="sell",
                price=ask_price,
                size=5.0,
                order_type="maker",
                reason=f"post-only ask level {lvl + 1}",
                prob_estimate=fair,
                ev_estimate=ask_price - fair,
                rule_snapshot=snapshot,
            ))
        return decisions
