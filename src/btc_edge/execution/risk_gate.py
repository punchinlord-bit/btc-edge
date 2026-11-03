"""Venue risk gate: price cap, fee-adjusted net EV, and fractional-Kelly sizing.

Implements the two quant-execution fixes:

* **Polymarket adverse selection** — hard price ceiling (never buy > 0.72) stops
  the "85-90c lock" trap where one wick wipes out 8 prior wins.
* **Kalshi fee churn** — a fee-adjusted minimum net EV (+4.5%) stops the model from
  firing tiny low-conviction bets that the taker fee + half-spread eats alive.

A signal below the minimum bet dollars (or a venue with a $0 balance) flips into
``SHADOW`` mode: it is logged and later graded (Brier) but never routed to the
exchange.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    mode: str  # "LIVE" | "SHADOW" | "SHADOW_FILTERED"
    reason: str
    side: str  # "up" | "down"
    entry_price: float
    win_prob: float
    friction: float = 0.0
    net_edge: float = 0.0
    contracts: int = 0
    cost_dollars: float = 0.0


class RiskController:
    """Shared risk + tuning engine (applies Kalshi discipline to Polymarket too)."""

    def __init__(
        self,
        max_contract_price: float = 0.72,
        polymarket_max_contract_price: float = 0.60,
        min_contract_price: float = 0.005,
        min_net_edge: float = 0.045,
        kelly_fraction: float = 0.15,
        max_bankroll_pct: float = 0.03,
        min_bet_dollars: float = 2.00,
        shadow_virtual_bankroll: float = 500.00,
        certainty_all_in_pct: float = 0.50,
        certainty_min_price: float = 0.93,
        certainty_min_win_prob: float = 0.97,
    ) -> None:
        self.max_contract_price = max_contract_price  # HARD CAP: never buy >72c
        # Polymarket gets a TIGHTER price ceiling than Kalshi: only enter when the
        # contract is genuinely cheap (value), never chase a 70-90c first bid.
        self.polymarket_max_contract_price = polymarket_max_contract_price
        self.min_contract_price = min_contract_price  # avoid lotto-ticket dust
        self.min_net_edge = min_net_edge  # require +4.5% net edge AFTER fee/spread
        self.kelly_fraction = kelly_fraction  # conservative fractional Kelly
        self.max_bankroll_pct = max_bankroll_pct  # max 3% of bankroll per 15m tick
        self.min_bet_dollars = min_bet_dollars  # minimum real bet size
        self.shadow_virtual_bankroll = shadow_virtual_bankroll  # virtual $ when balance $0
        # Near-certain all-in: the late-scalp fires at 93-97c in the final seconds
        # with the model >=97% confident. In that narrow case the 0.72 price cap and
        # the 3% Kelly cap are BOTH lifted, and the bot sizes to certainty_all_in_pct
        # of the live balance (default 50%, never 100%).
        self.certainty_all_in_pct = certainty_all_in_pct
        self.certainty_min_price = certainty_min_price
        self.certainty_min_win_prob = certainty_min_win_prob

    def estimate_friction(self, venue: str, ask_price: float, bid_price: float) -> float:
        """Fee + half-spread drag per contract.

        Kalshi taker fee peaks at 0.07 * P * (1-P); Polymarket has a lighter direct
        fee but wider book-sweep slippage on 15m markets.
        """
        half_spread = max(0.005, (ask_price - bid_price) / 2.0)
        if venue == "kalshi":
            return 0.07 * ask_price * (1.0 - ask_price) + half_spread
        return 0.02 * ask_price * (1.0 - ask_price) + half_spread

    def evaluate_signal(
        self,
        *,
        venue: str,
        model_prob_yes: float,
        yes_ask: float,
        yes_bid: float,
        no_ask: float,
        no_bid: float,
        live_balance: float,
    ) -> RiskDecision:
        buy_yes = model_prob_yes >= 0.5
        win_prob = model_prob_yes if buy_yes else (1.0 - model_prob_yes)
        entry_price = yes_ask if buy_yes else no_ask
        opp_bid = yes_bid if buy_yes else no_bid
        side = "up" if buy_yes else "down"

        # Near-certain all-in: the late-scalp fires at 93-97c in the final seconds
        # with the model >=97% confident. This is the narrow "super safe" case where
        # the 0.72 price cap and the 3% Kelly cap are lifted and the bot sizes up.
        certainty = (entry_price >= self.certainty_min_price) and (win_prob >= self.certainty_min_win_prob)

        # $0 / below-min balance -> SHADOW (paper) mode.
        is_shadow = live_balance < self.min_bet_dollars
        effective_bankroll = self.shadow_virtual_bankroll if is_shadow else live_balance

        # GATE 1: hard price ceiling & floor. Polymarket uses its own, tighter
        # ceiling so it only enters cheap (value) instead of throwing 70-90c at the
        # first bid; Kalshi keeps the standard 72c cap.
        max_price = self.polymarket_max_contract_price if venue == "polymarket" else self.max_contract_price
        if entry_price > max_price and not certainty:
            return RiskDecision(
                False, "SHADOW_FILTERED",
                f"PRICE_CAP_EXCEEDED ({entry_price:.2f} > {max_price:.2f})",
                side, entry_price, win_prob,
            )
        if entry_price < self.min_contract_price:
            return RiskDecision(
                False, "SHADOW_FILTERED",
                f"PRICE_FLOOR_BREACHED ({entry_price:.2f} < {self.min_contract_price})",
                side, entry_price, win_prob,
            )

        # GATE 2: fee-adjusted expected value. The certainty all-in (near-certain in
        # the final seconds) only needs a POSITIVE net edge — a 97%-confident 95c
        # entry has a thin (~0.7%) edge but a very high win rate, so the 4.5% churn
        # guard (which protects against low-conviction 70-90c churn) doesn't apply.
        friction = self.estimate_friction(venue, entry_price, opp_bid)
        effective_cost = entry_price + friction
        net_edge = win_prob - effective_cost
        min_edge = 0.0 if certainty else self.min_net_edge
        if net_edge < min_edge:
            return RiskDecision(
                False, "SHADOW_FILTERED",
                f"INSUFFICIENT_NET_EDGE ({net_edge * 100:.2f}% < {min_edge * 100:.2f}%)",
                side, entry_price, win_prob, friction, net_edge,
            )

        # GATE 3: fractional Kelly sizing with hard bankroll cap (certainty all-in
        # overrides the cap with a larger, but still bounded, fraction).
        if certainty:
            dollar_allocation = round(effective_bankroll * self.certainty_all_in_pct, 2)
        else:
            raw_kelly = max(0.0, net_edge / (1.0 - effective_cost))
            scaled_kelly_pct = min(raw_kelly * self.kelly_fraction, self.max_bankroll_pct)
            dollar_allocation = round(effective_bankroll * scaled_kelly_pct, 2)
        contracts = int(dollar_allocation // entry_price)
        actual_cost = round(contracts * entry_price, 2)

        if contracts <= 0:
            return RiskDecision(
                False, "SHADOW_FILTERED", "ZERO_CONTRACTS_AFTER_SIZING",
                side, entry_price, win_prob, friction, net_edge,
            )

        return RiskDecision(
            True, "SHADOW" if is_shadow else "LIVE", "PASSED_ALL_GATES",
            side, entry_price, round(win_prob, 4), round(friction, 4),
            round(net_edge, 4), contracts, actual_cost,
        )

    def evaluate_decision(
        self,
        *,
        venue: str,
        side: str,
        win_prob: float,
        entry_price: float,
        live_balance: float,
    ) -> RiskDecision:
        """Evaluate a strategy decision directly (side + win-prob + entry price).

        Reconstructs a minimal symmetric 2-cent book from the entry price so the
        fee-adjusted net-edge and price-cap gates apply uniformly to every entry.
        """
        if side == "up":
            yes_ask = entry_price
            yes_bid = max(0.0, entry_price - 0.02)
            no_ask = 1.0 - yes_bid
            no_bid = 1.0 - yes_ask
            model_prob_yes = win_prob
        else:
            no_ask = entry_price
            no_bid = max(0.0, entry_price - 0.02)
            yes_ask = 1.0 - no_bid
            yes_bid = 1.0 - no_ask
            model_prob_yes = 1.0 - win_prob
        return self.evaluate_signal(
            venue=venue,
            model_prob_yes=model_prob_yes,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            live_balance=live_balance,
        )

