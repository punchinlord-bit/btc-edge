"""Strategy 4: Deribit IV Option-Pricing Arb.

Triggers when |option_implied_prob - market_mid_price| >= 2% divergence and
spot orderflow (micro_acceleration / book_imbalance) confirms direction.
"""

from __future__ import annotations

from btc_edge.features.pattern_engine import Condition, Operand, RuleGroup
from btc_edge.strategies.base import MarketContext, Strategy, StrategyDecision


class IvMispricingStrategy(Strategy):
    name = "iv_mispricing"

    def __init__(self, min_divergence: float = 0.02) -> None:
        self.min_divergence = min_divergence
        self.rule = self._build_rule()

    def _build_rule(self) -> RuleGroup:
        # |option_implied_prob - contract_price| >= min_divergence, expressed as
        # two OR'd absolute-value conditions.
        return RuleGroup(
            logical_op="OR",
            conditions=[
                Condition(
                    Operand("option_implied_prob"),
                    ">=",
                    Operand("contract_price", scale=1.0, offset=self.min_divergence),
                ),
                Condition(
                    Operand("option_implied_prob"),
                    "<=",
                    Operand("contract_price", scale=1.0, offset=-self.min_divergence),
                ),
            ],
        )

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        divergence = ctx.option_implied_prob - ctx.contract_price
        if abs(divergence) < self.min_divergence:
            return []

        # Orderflow confirmation: the book and micro-momentum should agree with the
        # direction of the mispricing. A LARGE divergence (>=5%) overrides a flat
        # orderflow (choppy markets where micro-acceleration ~ 0), so a genuine
        # N(d2) vs market mispricing still trades instead of being filtered to nothing.
        orderflow = ctx.book_imbalance + ctx.micro_acceleration
        strong = abs(divergence) >= 0.05
        snapshot = self.rule.snapshot(ctx.features())

        decisions: list[StrategyDecision] = []
        if divergence > 0 and (orderflow > 0 or strong):
            # Market underprices the up contract; buy it.
            decisions.append(StrategyDecision(
                strategy=self.name,
                side="up",
                action="buy",
                price=ctx.best_ask,
                size=5.0,
                order_type="taker",
                reason="IV arb: market underpriced vs N(d2)",
                prob_estimate=ctx.option_implied_prob,
                ev_estimate=divergence,
                rule_snapshot=snapshot,
            ))
        elif divergence < 0 and (orderflow < 0 or strong):
            # Market overprices the up contract. Instead of shorting YES (which
            # needs USDC margin the account doesn't have), buy the NO leg — the
            # economically identical "down" bet that only costs USD.
            down_ask = 1.0 - ctx.best_bid
            decisions.append(StrategyDecision(
                strategy=self.name,
                side="down",
                action="buy",
                price=round(down_ask, 2),
                size=5.0,
                order_type="taker",
                reason="IV arb: market overpriced vs N(d2)",
                prob_estimate=1.0 - ctx.option_implied_prob,
                ev_estimate=-divergence,
                rule_snapshot=snapshot,
            ))
        return decisions
