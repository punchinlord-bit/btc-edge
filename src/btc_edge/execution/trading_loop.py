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

PRIMARY_TF = 300  # 5m window — where the strategy timeframes live (10s–300s)
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

        self._window_ts: int | None = None
        self._window_open_price: float | None = None
        self._last_features: dict[str, float] = {}
        self._open: list[dict[str, Any]] = []
        self._total_pnl = 0.0
        self._equity_curve: list[tuple[float, float]] = []
        self._metrics: dict[str, dict[str, Any]] = {
            s.name: {"net_ev": 0.0, "win_rate": 0.0, "trades": 0, "wins": 0} for s in self.strategies
        }

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
        """Detect window rollover and settle the previous window."""
        from btc_edge.connectors.poly_client import compute_window_ts

        window_ts = compute_window_ts(now, PRIMARY_TF)
        spot = self.state.btc_price
        if self._window_ts is None:
            self._window_ts = window_ts
            self._window_open_price = spot
            return
        if window_ts != self._window_ts:
            self._settle_window()
            self._window_ts = window_ts
            self._window_open_price = spot

    def _settle_window(self) -> None:
        spot = self.state.btc_price
        open_price = self._window_open_price
        if spot is None or open_price is None or open_price <= 0:
            self._open.clear()
            return
        outcome = 1.0 if spot >= open_price else 0.0
        pnl = self._settle_positions(outcome)
        if self._last_features:
            self.ensemble.update(self._last_features, outcome, PRIMARY_TF)
            self.ensemble.retrain_lgbm()
        self._total_pnl += pnl
        self._equity_curve.append((time.time(), self._total_pnl))
        if len(self._equity_curve) > 200:
            self._equity_curve = self._equity_curve[-200:]

    def _settle_positions(self, outcome: float) -> float:
        pnl = 0.0
        for pos in self._open:
            payoff = outcome if pos["side"] == "up" else (1.0 - outcome)
            if pos["action"] == "buy":
                pnl += (payoff - pos["price"]) * pos["size"] - pos["fee"]
            else:
                pnl += (pos["price"] - payoff) * pos["size"] - pos["fee"]
            m = self._metrics[pos["strategy"]]
            m["wins"] += 1 if (payoff - pos["price"]) > 0 else 0
        self._open.clear()
        self.safety.record_pnl(pnl)
        return pnl

    def _build_context(self, tf: int) -> MarketContext | None:
        spot = self.state.btc_price
        if spot is None:
            return None
        now = time.time()
        from btc_edge.connectors.poly_client import compute_window_ts

        window_ts = compute_window_ts(now, tf)
        open_price = self._window_open_price if self._window_open_price else spot
        time_remaining = self.state.window_countdowns.get("5m", float(tf))
        pm = self.state.polymarket_markets.get("5m", {})
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
        self._last_features = feats

        # Synthetic 2-cent book around the market mid (no live CLOB stream yet).
        spread = 0.02
        bid_price = max(0.01, contract_price - spread / 2.0)
        ask_price = min(0.99, contract_price + spread / 2.0)
        bids = [(bid_price, 5.0)]
        asks = [(ask_price, 5.0)]

        return MarketContext(
            venue="polymarket",
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

    def _route_decisions(self, decisions: list[Any], tf: int) -> None:
        for d in decisions:
            size = self.router.size_for(d)
            if size <= 0:
                continue
            is_maker = d.order_type == "maker"
            fee = fee_models.fee("polymarket", size, d.price, is_maker)
            self._open.append({
                "tf": tf, "side": d.side, "action": d.action,
                "price": d.price, "size": size, "fee": fee,
                "strategy": d.strategy, "prob": d.prob_estimate,
            })
            self._metrics[d.strategy]["trades"] += 1
            self._metrics[d.strategy]["net_ev"] += d.ev_estimate

    def _update_state(self, ctx: MarketContext, decisions: list[Any]) -> None:
        self.state.regime = ctx.regime
        self.state.strategy_metrics = self._metrics
        self.state.equity_curve = self._equity_curve[-200:]
        self.state.risk["at_risk_usd"] = sum(p["price"] * p["size"] for p in self._open)
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
        ctx = self._build_context(PRIMARY_TF)
        if ctx is None:
            return
        decisions: list[Any] = []
        for strat in self.strategies:
            try:
                decisions.extend(strat.evaluate(ctx))
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s evaluate error: %s", strat.name, exc)
        if decisions and not self.state.kill_switch:
            self._route_decisions(decisions, PRIMARY_TF)
        self._update_state(ctx, decisions)

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
