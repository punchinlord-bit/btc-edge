"""Three tagged intra-window strategies for the 15m BTC up/down market.

Each returns ``StrategyDecision`` objects tagged with a distinct ``strategy`` name
so the dashboard/widget can show *which* strategy fired each bet.
"""

from __future__ import annotations

from btc_edge.strategies.base import MarketContext, Strategy, StrategyDecision


def _side_price_prob(ctx: MarketContext) -> tuple[str, float, float]:
    """Return (side, order_price, prob_for_side) for the model's directional call."""
    p = ctx.prob_estimate
    if p >= 0.5:
        # Buy YES at the YES ask.
        return "up", round(ctx.best_ask, 2), p
    # Buy NO: the NO ask = 1 - YES bid.
    return "down", round(1.0 - ctx.best_bid, 2), 1.0 - p


class EarlyScalpStrategy(Strategy):
    """EARLY_SCALP (T-840s..T-360s): buy 2-4 shares at 32-54c on early dips."""

    name = "EARLY_SCALP"

    def __init__(
        self,
        min_time_remaining: float = 360.0,
        max_time_remaining: float = 840.0,
        min_price: float = 0.32,
        max_price: float = 0.54,
        min_edge: float = 0.01,
    ) -> None:
        self.min_time_remaining = min_time_remaining
        self.max_time_remaining = max_time_remaining
        self.min_price = min_price
        self.max_price = max_price
        self.min_edge = min_edge

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        if not (self.min_time_remaining <= ctx.time_remaining <= self.max_time_remaining):
            return []
        if not (self.min_price <= ctx.contract_price <= self.max_price):
            return []
        side, price, prob_for_side = _side_price_prob(ctx)
        edge = prob_for_side - price
        if edge < self.min_edge:
            return []
        return [StrategyDecision(
            strategy=self.name,
            side=side,
            action="buy",
            price=price,
            size=2.0,
            order_type="taker",
            reason="early orderbook exhaustion dip",
            prob_estimate=prob_for_side,
            ev_estimate=edge,
        )]


class MomentumBreakStrategy(Strategy):
    """MOMENTUM_BREAK (T-540s..T-180s): BTC breaks strike, BRTI leads >$2.50."""

    name = "MOMENTUM_BREAK"

    def __init__(
        self,
        min_time_remaining: float = 180.0,
        max_time_remaining: float = 540.0,
        min_lead: float = 5.0,
        min_edge: float = 0.03,
    ) -> None:
        self.min_time_remaining = min_time_remaining
        self.max_time_remaining = max_time_remaining
        self.min_lead = min_lead
        self.min_edge = min_edge

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        if not (self.min_time_remaining <= ctx.time_remaining <= self.max_time_remaining):
            return []
        # Kalshi BRTI must lead Polymarket by >$2.50 (clean momentum break).
        if abs(ctx.brti_lead) < self.min_lead:
            return []
        side = "up" if ctx.brti_lead > 0 else "down"
        p = ctx.prob_estimate
        prob_for_side = p if side == "up" else (1.0 - p)
        price = ctx.best_ask if side == "up" else (1.0 - ctx.best_bid)
        edge = prob_for_side - price
        if edge < self.min_edge:
            return []
        return [StrategyDecision(
            strategy=self.name,
            side=side,
            action="buy",
            price=price,
            size=3.0,
            order_type="taker",
            reason=f"momentum break (BRTI lead {ctx.brti_lead:+.2f})",
            prob_estimate=prob_for_side,
            ev_estimate=edge,
        )]


class LateSnipeStrategy(Strategy):
    """LATE_SNIPE (T-180s..T-50s): model >72% and ask mispriced >= +5% edge."""

    name = "LATE_SNIPE"

    def __init__(
        self,
        min_time_remaining: float = 50.0,
        max_time_remaining: float = 180.0,
        min_prob: float = 0.72,
        min_edge: float = 0.05,
    ) -> None:
        self.min_time_remaining = min_time_remaining
        self.max_time_remaining = max_time_remaining
        self.min_prob = min_prob
        self.min_edge = min_edge

    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        if not (self.min_time_remaining <= ctx.time_remaining <= self.max_time_remaining):
            return []
        side, price, prob_for_side = _side_price_prob(ctx)
        if prob_for_side < self.min_prob:
            return []
        edge = prob_for_side - price
        if edge < self.min_edge:
            return []
        return [StrategyDecision(
            strategy=self.name,
            side=side,
            action="buy",
            price=price,
            size=2.0,
            order_type="taker",
            reason=f"late snipe (P={prob_for_side:.0%}, edge={edge:+.0%})",
            prob_estimate=prob_for_side,
            ev_estimate=edge,
        )]
