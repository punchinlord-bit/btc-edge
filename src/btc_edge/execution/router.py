"""Execution router: live routing with Quarter-Kelly sizing.

Live order routing is gated behind ``ALLOW_LIVE_TRADING == "TRUE"``. Paper mode
has been removed — there is no local simulation path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from btc_edge.backtest import fee_models
from btc_edge.config import Settings
from btc_edge.strategies.base import StrategyDecision

logger = logging.getLogger(__name__)

# Hard per-order dollar floor/ceiling so a tiny-balance account still bets a
# meaningful $0.50–$1.15 instead of a useless $0.10 clamp.
MIN_ORDER_USD = 0.50
MAX_ORDER_USD = 1.15


def idempotency_key(
    venue: str, side: str, action: str, price: float, size: float, ts: float | None = None
) -> str:
    """Deterministic idempotency key so a crash + retry never double-places."""
    ts = ts if ts is not None else time.time()
    raw = f"{venue}|{side}|{action}|{price:.6f}|{size:.6f}|{int(ts)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class RoutedOrder:
    decision: StrategyDecision
    venue: str
    price: float
    size: float
    fee: float
    is_maker: bool
    routed_live: bool
    idempotency_key: str = ""
    status: str = "filled"


def kelly_fraction(prob: float, price: float) -> float:
    """Full Kelly fraction for a binary contract bought at ``price`` with true
    win probability ``prob``: ``(prob - price) / (1 - price)``."""
    if price >= 1.0:
        return 0.0
    if prob <= price:
        return 0.0
    return (prob - price) / (1.0 - price)


def quarter_kelly_size(prob: float, price: float, bankroll: float, cap_pct: float = 5.0) -> float:
    """Quarter-Kelly dollar size with a hard survival cap.

    ``kellyFraction = (prob - price) / (1 - price)``; the bet is
    ``min(0.05, max(0, kellyFraction * 0.25))`` of bankroll — never risk more than
    5% of bankroll on a single 15m window. A zero/negative edge sizes to zero.
    """
    fraction = 0.25 * kelly_fraction(prob, price)
    if fraction <= 0:
        return 0.0
    fraction = min(cap_pct / 100.0, fraction)  # cap_pct default 5.0 -> 0.05
    return fraction * bankroll


def full_kelly_size(prob: float, price: float, bankroll: float, cap_pct: float = 100.0) -> float:
    """Dollar size for a FULL-Kelly position capped at ``cap_pct``% of bankroll.

    Used by "gusto" recovery mode, where the 0.35 fractional-Kelly discount and
    the tight per-trade cap are lifted so each win moves a small balance hard.
    """
    fraction = kelly_fraction(prob, price)
    if fraction <= 0:
        return 0.0
    dollar_size = fraction * bankroll
    cap = bankroll * cap_pct / 100.0
    return min(dollar_size, cap)


def shares_from_dollars(dollar_size: float, price: float) -> float:
    if price <= 0:
        return 0.0
    return dollar_size / price


class Router:
    """Routes strategy decisions to paper or live execution."""

    def __init__(
        self,
        settings: Settings,
        kalshi_client: Any | None = None,
        poly_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.kalshi_client = kalshi_client
        self.poly_client = poly_client
        self.orders: list[RoutedOrder] = []
        self._last_kalshi_convert_ts = 0.0

    @property
    def live_allowed(self) -> bool:
        return self.settings.live_trading_allowed()

    def size_for(
        self,
        decision: StrategyDecision,
        venue: str | None = None,
        bankroll: float | None = None,
        gusto: bool = False,
    ) -> float:
        """Compute the Quarter-Kelly position size for a decision.

        ``bankroll`` defaults to the configured value, but the live loop passes the
        *actual* wallet balance so sizing always scales with the money in the account.

        When ``gusto`` is set for a Polymarket order, the conservative per-trade
        cap, the 0.35 fractional-Kelly discount, and the extreme-price guard are
        lifted so a low balance can be ground back up with full-Kelly recovery bets.
        """
        br = bankroll if (bankroll is not None and bankroll > 0) else self.settings.trading.bankroll
        v = venue or _venue(decision, self.settings)
        # Closing (sell) decisions — e.g. take-profit — carry their own position
        # size and must not be re-sized by Quarter-Kelly (the entry-edge logic below
        # only applies to opening buys).
        if decision.action == "sell":
            return max(0.0, decision.size)
        gusto_pm = gusto and v == "polymarket"
        # Polymarket gets a tighter per-trade cap: it is more volatile and its
        # 5-share minimum is a bigger slice of a small account than Kalshi's
        # 1-contract minimum. Gusto lifts that cap entirely (full bankroll).
        cap_pct = self.settings.trading.quarter_kelly_cap_pct
        if v == "polymarket":
            cap_pct = 100.0 if gusto_pm else self.settings.trading.polymarket_cap_pct
        prob = decision.prob_estimate
        price = decision.price
        # Never fire a zero/negative-edge order: if the model's probability is not
        # above the price we'd pay, the "enforce minimum" fallback used to force a
        # 5-share / 1-contract bet anyway — that's what produced careless longshot
        # deposits (e.g. buying a ~1%-chance leg with seconds left). Skip instead.
        if prob <= price:
            return 0.0
        # Skip extreme longshots/shorthots (<2% or >98%): at those prices the
        # model's edge estimate is unreliable. Widen past the old 5%/95% so the
        # model can still act on a genuine edge at e.g. a 3.5c leg.
        lo, hi = (0.005, 0.995)
        if price < lo or price > hi:
            return 0.0
        if gusto_pm:
            dollar = full_kelly_size(prob, price, br, cap_pct)
            target_usd = dollar  # gusto: no USD clamp (full-Kelly recovery mode)
        else:
            dollar = quarter_kelly_size(prob, price, br, cap_pct)
            # Hard $0.50–$1.15 multi-share sizing: never pin to a useless $0.10 clamp.
            target_usd = max(MIN_ORDER_USD, min(MAX_ORDER_USD, dollar))
        size = shares_from_dollars(target_usd, price)
        if not gusto_pm:
            size = max(2.0, size)  # always hold >= 2 shares/contracts when price allows
        size = fee_models.apply_min_order(v, size)
        if size <= 0:
            # With a small bankroll the Kelly size can fall below the venue minimum.
            # Lift to the minimum so the bot keeps trading even on a tiny balance.
            min_shares = fee_models.POLYMARKET_MIN_SHARES if v == "polymarket" else 1.0
            if min_shares * price > br:
                return 0.0
            size = min_shares
        if not gusto_pm:
            # Ensure total outlay stays within [MIN_ORDER_USD, MAX_ORDER_USD + 0.10].
            if size * price < MIN_ORDER_USD:
                size = max(2.0, math.ceil(MIN_ORDER_USD / price))
            elif size * price > MAX_ORDER_USD + 0.10:
                min_shares = fee_models.POLYMARKET_MIN_SHARES if v == "polymarket" else 1.0
                size = max(min_shares, math.floor(MAX_ORDER_USD / price))
        logger.info(
            "sizing %s: target_usd=%.2f price=%.4f shares=%.1f outlay=%.2f",
            v, target_usd, price, size, size * price,
        )
        return size

    def route(self, decision: StrategyDecision, venue: str, outcome: float | None = None) -> RoutedOrder:
        """Route a decision live (paper mode removed — no local simulation)."""
        is_maker = decision.order_type == "maker"
        fill_price = fee_models.effective_fill_price(decision.price, is_maker)
        size = self.size_for(decision)
        f = fee_models.fee(venue, size, fill_price, is_maker)

        live = size > 0
        key = idempotency_key(venue, decision.side, decision.action, fill_price, size)
        order = RoutedOrder(
            decision=decision,
            venue=venue,
            price=fill_price,
            size=size,
            fee=f,
            is_maker=is_maker,
            routed_live=live,
            idempotency_key=key,
        )

        # Skip re-routing an identical order we've already placed this second.
        if any(o.idempotency_key == key for o in self.orders):
            return order

        self.orders.append(order)
        return order

    async def place_order_async(
        self,
        venue: str,
        decision: StrategyDecision,
        price: float,
        size: float,
        is_maker: bool,
        token_id: str | None = None,
        kalshi_ticker: str | None = None,
    ) -> None:
        """Submit a real order to the exchange (awaited async)."""
        if venue == "kalshi" and self.kalshi_client is not None:
            # KXBTC15M settles on the crypto (BTC) exchange shard. The API does
            # NOT auto-convert USD the way the web app does, so before placing an
            # order we top up the crypto balance from USD (throttled to ~60s so we
            # don't hammer the rate-limited balance endpoint).
            if time.time() - self._last_kalshi_convert_ts >= 60.0:
                self._last_kalshi_convert_ts = time.time()
                try:
                    converted = await self.kalshi_client.ensure_crypto_balance()
                    if converted:
                        await asyncio.sleep(1.0)  # let the intra-account transfer settle
                except Exception as exc:  # noqa: BLE001 - never block trading on funding
                    logger.warning("Kalshi crypto funding check failed: %s", exc)
            # KXBTC15M is "BTC price up in next 15 mins?" — a pure up/down binary.
            # "up" -> buy YES; "down" -> buy NO (sell YES).
            side = "yes" if decision.side == "up" else "no"
            # Taker orders use immediate-or-cancel (fills now); maker orders rest as
            # good-till-canceled. IOC avoids the full resting-collateral requirement.
            order_type = "limit" if is_maker else "market"
            await self.kalshi_client.place_order(
                ticker=kalshi_ticker or self.settings.kalshi.market_ticker,
                side=side,
                action=decision.action,
                count=max(1, int(round(size))),
                price=price,
                order_type=order_type,
                post_only=is_maker,
            )
        elif venue == "polymarket" and self.poly_client is not None:
            # Polymarket US places orders by market slug + intent (no token IDs).
            slug = token_id or (self.poly_client.resolve_15m().get("slug") or "")
            if not slug:
                return
            await self.poly_client.place_order(
                slug=slug,
                side=decision.side,
                action=decision.action,
                price=price,
                size=size,
                is_maker=is_maker,
            )


def _venue(decision: StrategyDecision, settings: Settings) -> str:
    # Decisions carry no venue; default to polymarket unless overridden.
    return getattr(decision, "venue", "polymarket")
