"""Strategy 2: Post-Only CLOB Market Making.

Active when time_remaining > 120s and regime is DAMPENING. Quotes 3 post-only
bid/ask levels around the ensemble fair value when the book spread >= $0.02,
refreshing every 15 seconds (or immediately on a >0.03% spot move).
"""

from __future__ import annotations

import time

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
        min_edge: float = 0.01,
    ) -> None:
        self.min_time_remaining = float(min_time_remaining)
        self.levels = levels
        self.min_spread = min_spread
        self.refresh_seconds = refresh_seconds
        self.spot_move_pct = spot_move_pct
        self.min_edge = min_edge
        self._last_quote_ts: dict[str, float] = {}
        self.rule = self._build_rule()

    def _build_rule(self) -> RuleGroup:
        return RuleGroup(
            logical_op="AND",
            conditions=[
                Condition(Operand("time_remaining"), ">", Operand.literal(self.min_time_remaining)),
            ],
        )

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        # Throttle per-venue: only re-quote each market every ``refresh_seconds``
        # (avoids flooding the book / dashboard with identical quotes every 2s tick,
        # while still quoting Kalshi and Polymarket independently).
        venue = getattr(ctx, "venue", "default")
        if time.time() - self._last_quote_ts.get(venue, 0.0) < self.refresh_seconds:
            return []
        snapshot = self.rule.snapshot(ctx.features())
        if not snapshot["result"]:
            return []

        spread = ctx.best_ask - ctx.best_bid
        if spread < self.min_spread:
            return []

        self._last_quote_ts[venue] = time.time()

        # Directional edge: bet the side where the model's estimate beats the
        # market's price. When the model thinks "up" is MORE likely than the market
        # prices it (prob_up > fair), buy "up"; when LESS, buy "down" (the NO leg).
        # This replaces the old ``prob_up >= 0.5`` gate, which ignored the market
        # and fired "up" even when the market was already pricing "up" higher.
        fair = ctx.contract_price
        prob_up = ctx.prob_estimate
        edge = prob_up - fair
        if edge >= self.min_edge:
            side, side_prob = "up", prob_up
        elif edge <= -self.min_edge:
            side, side_prob = "down", 1.0 - prob_up
        else:
            # No meaningful edge vs the market mid -> stand aside.
            return []
        decisions: list[StrategyDecision] = []
        for lvl in range(self.levels):
            # Post a maker BID *below the model's own fair value* (side_prob) on the
            # edge side, capturing half the spread when filled. The old code priced
            # off the market mid and bought ABOVE fair (at the ask), which is
            # structurally negative-EV; now the order always has positive edge.
            bid_price = max(0.01, round(side_prob - (lvl + 1) * (spread / 2.0), 2))
            decisions.append(StrategyDecision(
                strategy=self.name,
                side=side,
                action="buy",
                price=bid_price,
                size=5.0,
                order_type="maker",
                reason=f"bid {side} level {lvl + 1}",
                prob_estimate=side_prob,
                ev_estimate=side_prob - bid_price,
                rule_snapshot=snapshot,
            ))
        return decisions
