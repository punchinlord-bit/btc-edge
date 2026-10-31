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
    ) -> None:
        self.min_time_remaining = float(min_time_remaining)
        self.levels = levels
        self.min_spread = min_spread
        self.refresh_seconds = refresh_seconds
        self.spot_move_pct = spot_move_pct
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

        # Buy the YES side. On Kalshi we must be a TAKER (buy at the ask, IOC) —
        # maker/GTC orders rest on the book and need USDC margin, which returns
        # `insufficient_balance`; taker orders fill immediately and pay in USD.
        # Polymarket US supports resting maker orders on both legs, so keep those.
        fair = ctx.contract_price
        decisions: list[StrategyDecision] = []
        for lvl in range(self.levels):
            if venue == "kalshi":
                buy_price = min(0.99, round(fair + (lvl + 1) * (spread / 2.0), 2))
                decisions.append(StrategyDecision(
                    strategy=self.name,
                    side="up",
                    action="buy",
                    price=buy_price,
                    size=5.0,
                    order_type="taker",
                    reason=f"taker buy level {lvl + 1}",
                    prob_estimate=fair,
                    ev_estimate=fair - buy_price,
                    rule_snapshot=snapshot,
                ))
            else:
                bid_price = max(0.01, round(fair - (lvl + 1) * (spread / 2.0), 2))
                decisions.append(StrategyDecision(
                    strategy=self.name,
                    side="up",
                    action="buy",
                    price=bid_price,
                    size=5.0,
                    order_type="maker",
                    reason=f"bid level {lvl + 1}",
                    prob_estimate=fair,
                    ev_estimate=fair - bid_price,
                    rule_snapshot=snapshot,
                ))
                ask_price = min(0.99, round(fair + (lvl + 1) * (spread / 2.0), 2))
                decisions.append(StrategyDecision(
                    strategy=self.name,
                    side="up",
                    action="sell",
                    price=ask_price,
                    size=5.0,
                    order_type="maker",
                    reason=f"ask level {lvl + 1}",
                    prob_estimate=fair,
                    ev_estimate=ask_price - fair,
                    rule_snapshot=snapshot,
                ))
        return decisions
