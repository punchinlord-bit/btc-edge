"""Real-time trading loop: features → strategies → router → paper settlement.

Ties the live market feed into the four strategies. Each tick it computes
features (regime, ``N(d2)`` implied probability, window delta, book imbalance,
micro-acceleration), runs the ensemble for ``prob_estimate``, evaluates the
strategies, routes decisions through the paper/live router (Quarter-Kelly
sizing), settles closed windows, and mirrors results (signals, per-strategy
metrics, equity curve, regime, at-risk) into the dashboard state.

Paper-first: live routing stays blocked behind the dual gate in ``Settings``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from btc_edge.backtest import fee_models
from btc_edge.config import Settings
from btc_edge.execution.router import Router
from btc_edge.execution.safety import SafetyWatchdog
from btc_edge.features import indicators
from btc_edge.features.regime import RegimeClassifier, realized_vol_from_prices
from btc_edge.ml.model import ProbabilityEnsemble
from btc_edge.strategies.base import MarketContext
from btc_edge.strategies.crowd_follow import CrowdFollowStrategy
from btc_edge.strategies.iv_mispricing import IvMispricingStrategy
from btc_edge.strategies.late_scalp import LateScalpStrategy
from btc_edge.strategies.market_maker import MarketMakerStrategy
from btc_edge.web.app import EngineState
from btc_edge.web.live_feed import LiveMarketFeed

logger = logging.getLogger(__name__)

# (timeframe_seconds, venue, countdown_label). 5m trades on Polymarket, 15m on
# Kalshi — the bot actively trades BOTH windows every cycle.
TRADING_WINDOWS: tuple[tuple[int, str, str], ...] = (
    (300, "polymarket", "5m"),
    (900, "kalshi", "15m"),
)
IMPLIED_VOL_DEFAULT = 0.5  # annualized BTC IV proxy when Deribit DVOL is unavailable
FEED_SAMPLE_HZ = 1.0 / 3.0  # live feed polls ~every 3s


class TradingLoop:
    """Orchestrates the strategy → router → settlement cycle against live data."""

    def __init__(self, state: EngineState, settings: Settings, feed: LiveMarketFeed) -> None:
        self.state = state
        self.settings = settings
        self.feed = feed
        self.ensemble = ProbabilityEnsemble()
        self.regime_cls = RegimeClassifier(
            rv_iv_threshold=settings.features.vol_rv_iv_threshold,
            funding_zscore_threshold=settings.features.funding_zscore_threshold,
        )
        self.router = Router(settings)
        self.safety = SafetyWatchdog(
            bankroll=settings.trading.bankroll,
            max_daily_loss_pct=settings.trading.max_daily_loss_pct,
        )
        self.strategies = self._build_strategies(settings)

        self._windows: dict[int, dict[str, Any]] = {
            tf: {"window_ts": None, "open_price": None, "last_features": {}}
            for tf, _, _ in TRADING_WINDOWS
        }
        self._open: list[dict[str, Any]] = []
        self._total_pnl = 0.0
        self._equity_curve: list[tuple[float, float]] = []
        self._metrics: dict[str, dict[str, Any]] = {
            s.name: {
                "net_ev": 0.0,
                "win_rate": 0.0,
                "trades": 0,
                "wins": 0,
                "notional_usd": 0.0,
                "pnl_usd": 0.0,
                "fees_usd": 0.0,
            }
            for s in self.strategies
        }
        self._recent_trades: list[dict[str, Any]] = []

    def _build_strategies(self, settings: Settings) -> list[Any]:
        s = settings.strategies
        out: list[Any] = []
        if s.late_scalp.enabled:
            c = s.late_scalp
            out.append(LateScalpStrategy(
                min_time_remaining=c.min_time_remaining,
                max_time_remaining=c.max_time_remaining,
                min_prob=c.min_prob,
                max_prob=c.max_prob,
                min_window_delta_pct=c.min_window_delta_pct,
                min_ev_threshold=settings.trading.min_ev_threshold,
            ))
        if s.market_maker.enabled:
            c = s.market_maker
            out.append(MarketMakerStrategy(
                min_time_remaining=c.min_time_remaining,
                levels=c.levels,
                min_spread=c.min_spread,
                refresh_seconds=c.refresh_seconds,
                spot_move_pct=c.spot_move_pct,
            ))
        if s.crowd_follow.enabled:
            c = s.crowd_follow
            out.append(CrowdFollowStrategy(
                min_time_remaining=c.min_time_remaining,
                max_time_remaining=c.max_time_remaining,
                min_contract_price=c.min_contract_price,
            ))
        if s.iv_mispricing.enabled:
            out.append(IvMispricingStrategy(min_divergence=s.iv_mispricing.min_divergence))
        return out

    def _sync_window(self, now: float) -> None:
        """Detect rollover for each trading window and settle the expired one."""
        from btc_edge.connectors.poly_client import compute_window_ts

        spot = self.state.btc_price
        for tf, _, _ in TRADING_WINDOWS:
            w = self._windows[tf]
            window_ts = compute_window_ts(now, tf)
            if w["window_ts"] is None:
                w["window_ts"] = window_ts
                w["open_price"] = spot
                continue
            if window_ts != w["window_ts"]:
                self._settle_window(tf)
                w["window_ts"] = window_ts
                w["open_price"] = spot

    def _settle_window(self, tf: int) -> None:
        w = self._windows[tf]
        spot = self.state.btc_price
        open_price = w["open_price"]
        if spot is None or open_price is None or open_price <= 0:
            self._open = [p for p in self._open if p.get("tf") != tf]
            return
        outcome = 1.0 if spot >= open_price else 0.0
        pnl = self._settle_positions(tf, outcome)
        if w["last_features"]:
            self.ensemble.update(w["last_features"], outcome, tf)
            self.ensemble.retrain_lgbm()
        self._total_pnl += pnl
        self._equity_curve.append((time.time(), self._total_pnl))
        if len(self._equity_curve) > 200:
            self._equity_curve = self._equity_curve[-200:]

    def _settle_positions(self, tf: int, outcome: float) -> float:
        pnl = 0.0
        remaining: list[dict[str, Any]] = []
        for pos in self._open:
            if pos.get("tf") != tf:
                remaining.append(pos)
                continue
            payoff = outcome if pos["side"] == "up" else (1.0 - outcome)
            if pos["action"] == "buy":
                trade_pnl = (payoff - pos["price"]) * pos["size"] - pos["fee"]
            else:
                trade_pnl = (pos["price"] - payoff) * pos["size"] - pos["fee"]
            pnl += trade_pnl
            m = self._metrics[pos["strategy"]]
            m["wins"] += 1 if trade_pnl > 0 else 0
            m["pnl_usd"] += trade_pnl
            if m["trades"]:
                m["win_rate"] = m["wins"] / m["trades"]
            self._recent_trades.append({
                "ts": time.time(),
                "tf": tf,
                "venue": pos.get("venue", ""),
                "strategy": pos["strategy"],
                "side": pos["side"],
                "action": pos["action"],
                "price": pos["price"],
                "size": pos["size"],
                "notional": pos.get("notional", pos["price"] * pos["size"]),
                "fee": pos["fee"],
                "pnl": trade_pnl,
            })
        self._open = remaining
        self._recent_trades = self._recent_trades[-50:]
        self.safety.record_pnl(pnl)
        return pnl

    def _build_context(self, tf: int, venue: str, label: str) -> MarketContext | None:
        spot = self.state.btc_price
        if spot is None:
            return None
        now = time.time()
        from btc_edge.connectors.poly_client import compute_window_ts

        window_ts = compute_window_ts(now, tf)
        w = self._windows[tf]
        open_price = w["open_price"] if w["open_price"] else spot
        time_remaining = self.state.window_countdowns.get(label, float(tf))

        if venue == "kalshi":
            kal = self.state.kalshi_market or {}
            contract_price = float(kal.get("prob_up") or kal.get("last_price") or 0.5)
        else:
            pm = self.state.polymarket_markets.get(label, {})
            contract_price = float(pm.get("prob_up") or pm.get("up") or 0.5)

        prices = [p for _, p in self.state.price_history]
        rvol = realized_vol_from_prices(prices, sample_hz=FEED_SAMPLE_HZ) if len(prices) >= 2 else 0.0
        implied = IMPLIED_VOL_DEFAULT
        regime = self.regime_cls.classify_raw(rvol / implied if implied > 0 else 0.0)
        regime_str = str(regime.value)

        window_delta = indicators.window_delta_pct(spot, open_price)
        option_prob = indicators.option_implied_prob(spot, open_price, implied, time_remaining)
        book_imb = 0.0
        micro = 0.0

        feats: dict[str, float] = {
            "time_remaining": time_remaining,
            "contract_price": contract_price,
            "option_implied_prob": option_prob,
            "window_delta_pct": window_delta,
            "book_imbalance": book_imb,
            "micro_acceleration": micro,
            "news_sentiment": self.state.news_sentiment,
            "realized_vol": rvol,
            "implied_vol": implied,
            "current_spot": spot,
            "window_open_price": open_price,
            "regime_dampening": 1.0 if regime_str == "DAMPENING" else 0.0,
            "regime_amplifying": 1.0 if regime_str == "AMPLIFYING" else 0.0,
        }
        prob = self.ensemble.predict(feats, tf)
        w["last_features"] = feats

        # Synthetic 2-cent book around the market mid (no live CLOB stream yet).
        spread = 0.02
        bid_price = max(0.01, contract_price - spread / 2.0)
        ask_price = min(0.99, contract_price + spread / 2.0)
        bids = [(bid_price, 5.0)]
        asks = [(ask_price, 5.0)]

        return MarketContext(
            venue=venue,
            timeframe_seconds=tf,
            window_ts=window_ts,
            window_open_price=open_price,
            current_spot=spot,
            time_remaining=time_remaining,
            contract_price=contract_price,
            prob_estimate=prob,
            window_delta_pct=window_delta,
            option_implied_prob=option_prob,
            book_imbalance=book_imb,
            micro_acceleration=micro,
            regime=regime_str,
            news_sentiment=self.state.news_sentiment,
            realized_vol=rvol,
            implied_vol=implied,
            bids=bids,
            asks=asks,
        )

    def _route_decisions(self, decisions: list[Any], tf: int, venue: str) -> None:
        for d in decisions:
            size = self.router.size_for(d)
            if size <= 0:
                continue
            is_maker = d.order_type == "maker"
            fee = fee_models.fee(venue, size, d.price, is_maker)
            notional = d.price * size
            self._open.append({
                "tf": tf, "venue": venue, "side": d.side, "action": d.action,
                "price": d.price, "size": size, "fee": fee,
                "strategy": d.strategy, "prob": d.prob_estimate,
                "notional": notional, "ts": time.time(),
            })
            m = self._metrics[d.strategy]
            m["trades"] += 1
            m["net_ev"] += d.ev_estimate
            m["notional_usd"] += notional
            m["fees_usd"] += fee

    def _update_state(self, ctx: MarketContext, decisions: list[Any]) -> None:
        self.state.regime = ctx.regime
        self.state.strategy_metrics = self._metrics
        self.state.equity_curve = self._equity_curve[-200:]
        self.state.risk["at_risk_usd"] = sum(p["price"] * p["size"] for p in self._open)
        self.state.recent_trades = self._recent_trades[-50:]
        self.state.trade_totals = {
            "total_trades": sum(m["trades"] for m in self._metrics.values()),
            "total_notional_usd": sum(m["notional_usd"] for m in self._metrics.values()),
            "total_pnl_usd": self._total_pnl,
            "total_fees_usd": sum(m["fees_usd"] for m in self._metrics.values()),
        }
        if decisions:
            self.state.signals = [
                f"{d.strategy}: {d.action} {d.side} @ {d.price:.3f} (EV {d.ev_estimate:+.3f})"
                for d in decisions[:6]
            ]
        else:
            tone = "bullish" if self.state.news_sentiment > 0.15 else (
                "bearish" if self.state.news_sentiment < -0.15 else "neutral"
            )
            self.state.signals = [f"No edge yet — news is {tone}. Waiting for a setup."]

    async def tick(self) -> None:
        if self.state.btc_price is None:
            return
        now = time.time()
        self._sync_window(now)
        all_decisions: list[Any] = []
        last_ctx: MarketContext | None = None
        for tf, venue, label in TRADING_WINDOWS:
            ctx = self._build_context(tf, venue, label)
            if ctx is None:
                continue
            last_ctx = ctx
            decisions: list[Any] = []
            for strat in self.strategies:
                try:
                    decisions.extend(strat.evaluate(ctx))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("%s evaluate error: %s", strat.name, exc)
            if decisions and not self.state.kill_switch:
                self._route_decisions(decisions, tf, venue)
            all_decisions.extend(decisions)

        if last_ctx is not None:
            self._update_state(last_ctx, all_decisions)

        # Safety: bridge the watchdog to the shared dashboard kill switch.
        self.safety.state.kill_switch = self.state.kill_switch
        if self.safety.daily_loss_limit_breached():
            self.state.kill_switch = True
            self.safety.trigger_kill_switch()

    async def run(self, interval: float = 2.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                logger.warning("Trading loop error: %s", exc)
            await asyncio.sleep(interval)
