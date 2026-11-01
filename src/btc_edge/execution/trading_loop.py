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
import contextlib
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
from btc_edge.ml.strategy_registry import Registry
from btc_edge.strategies.base import MarketContext
from btc_edge.strategies.crowd_follow import CrowdFollowStrategy
from btc_edge.strategies.iv_mispricing import IvMispricingStrategy
from btc_edge.strategies.late_scalp import LateScalpStrategy
from btc_edge.strategies.market_maker import MarketMakerStrategy
from btc_edge.web.app import EngineState
from btc_edge.web.live_feed import LiveMarketFeed

logger = logging.getLogger(__name__)

# (timeframe_seconds, venue, countdown_label). The bot actively trades every
# window: 5m on Polymarket, 15m on BOTH Polymarket and Kalshi. Each
# (venue, timeframe) pair trains its own ensemble so the model tracks each
# market independently.
TRADING_WINDOWS: tuple[tuple[int, str, str], ...] = (
    (900, "polymarket", "15m"),  # Polymarket US cpc-btc-updown-15m up/down binary
    (900, "kalshi", "15m"),  # KXBTC15M = "BTC price up in next 15 mins?" up/down binary
)
IMPLIED_VOL_DEFAULT = 0.5  # annualized BTC IV proxy when Deribit DVOL is unavailable
FEED_SAMPLE_HZ = 1.0 / 3.0  # live feed polls ~every 3s


class TradingLoop:
    """Orchestrates the strategy → router → settlement cycle against live data."""

    def __init__(
        self,
        state: EngineState,
        settings: Settings,
        feed: LiveMarketFeed,
        kalshi_client: Any | None = None,
        poly_client: Any | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.state = state
        self.settings = settings
        self.feed = feed
        self.registry = registry or Registry()
        self.kalshi_client = kalshi_client
        self._ensembles: dict[tuple[int, str], ProbabilityEnsemble] = {
            (tf, venue): ProbabilityEnsemble() for tf, venue, _ in TRADING_WINDOWS
        }
        self.regime_cls = RegimeClassifier(
            rv_iv_threshold=settings.features.vol_rv_iv_threshold,
            funding_zscore_threshold=settings.features.funding_zscore_threshold,
        )
        self.router = Router(settings, kalshi_client=kalshi_client, poly_client=poly_client)
        self.safety = SafetyWatchdog(
            bankroll=settings.trading.bankroll,
            max_daily_loss_pct=settings.trading.max_daily_loss_pct,
        )
        self.strategies = self._build_strategies(settings)

        # Out-of-band Telegram alerts for real order fills (throttled to avoid spam).
        from btc_edge.execution.alerts import build_alerter

        self._alerter = build_alerter(settings)
        self._last_alert_ts = 0.0
        # Track which (window_end, venue) we've already sent a "last minute" heads-up
        # for, so each 15m window fires exactly one notification per venue.
        self._notified_last_minute: set[tuple[int, str]] = set()
        # Last ensemble signal direction per venue (for flip detection).
        self._last_signal_dir: dict[str, str] = {}

        self._windows: dict[tuple[int, str], dict[str, Any]] = {
            (tf, venue): {"window_ts": None, "open_price": None, "last_features": {}}
            for tf, venue, _ in TRADING_WINDOWS
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
                "losses": 0,
                "notional_usd": 0.0,
                "pnl_usd": 0.0,
                "fees_usd": 0.0,
            }
            for s in self.strategies
        }
        self._recent_trades: list[dict[str, Any]] = []
        self._venue_stats: dict[str, dict[str, Any]] = {
            v: {"notional_usd": 0.0, "pnl_usd": 0.0, "trades": 0, "wins": 0, "losses": 0}
            for v in ("polymarket", "kalshi")
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
        """Detect rollover for each trading window and settle the expired one."""
        from btc_edge.connectors.poly_client import compute_window_ts

        spot = self.state.btc_price
        for tf, venue, _ in TRADING_WINDOWS:
            w = self._windows[(tf, venue)]
            window_ts = compute_window_ts(now, tf)
            if w["window_ts"] is None:
                w["window_ts"] = window_ts
                w["open_price"] = spot
                continue
            if window_ts != w["window_ts"]:
                self._settle_window(tf, venue)
                w["window_ts"] = window_ts
                w["open_price"] = spot

    def _settle_window(self, tf: int, venue: str) -> None:
        w = self._windows[(tf, venue)]
        spot = self.state.btc_price
        open_price = w["open_price"]
        if spot is None or open_price is None or open_price <= 0:
            self._open = [p for p in self._open if not (p.get("tf") == tf and p.get("venue") == venue)]
            return
        outcome = 1.0 if spot >= open_price else 0.0
        pnl = self._settle_positions(tf, venue, outcome)
        if w["last_features"]:
            ens = self._ensembles[(tf, venue)]
            self._record_and_gate(ens, tf, venue, w["last_features"], outcome, open_price, spot)
        self._total_pnl += pnl
        self._equity_curve.append((time.time(), self._total_pnl))
        if len(self._equity_curve) > 200:
            self._equity_curve = self._equity_curve[-200:]

    def _record_and_gate(
        self,
        ens: ProbabilityEnsemble,
        tf: int,
        venue: str,
        features: dict[str, Any],
        outcome: float,
        open_price: float,
        spot: float,
    ) -> None:
        """Record the resolved window as an OOS sample, then gate the model update.

        The online logistic update becomes a CHALLENGER; it is only applied when the
        promotion gate clears (or when there is not yet enough OOS data to judge).
        """
        from btc_edge.ml.model import feature_dict
        from btc_edge.ml.promotion_gate import (
            CHAMPION,
            StrategyVersion,
            WindowEvaluation,
            classify_3regime,
        )

        normalized = feature_dict(features, tf)
        abs_move_pct = abs(spot - open_price) / open_price * 100.0
        regime = classify_3regime(abs_move_pct)

        self.registry.record_window(WindowEvaluation(
            window_start_sec=(int(time.time()) // tf) * tf,
            strike_price=open_price,
            close_price=spot,
            outcome=1 if outcome >= 0.5 else 0,
            regime=regime,
            market_key=venue,  # venue-scoped OOS sample (kalshi vs polymarket)
            features=normalized,
            market_ask_up=0.52,
            market_ask_down=0.52,
            fee_rate=0.015,
        ))

        champion_config = ens.logreg_weights()
        challenger_config = ens.proposed_logreg_weights(features, outcome, tf)

        apply_update = True
        if champion_config and challenger_config:
            champion = StrategyVersion(
                version_id=f"{venue}-{tf}-champion", status=CHAMPION,
                market_key=venue, config=champion_config,
            )
            challenger = StrategyVersion(
                version_id=f"{venue}-{tf}-{int(time.time())}",
                market_key=venue, config=challenger_config,
            )
            # Evaluate the challenger ONLY against this venue's OOS windows so a
            # Kalshi update is never judged on Polymarket outcomes (and vice versa).
            venue_windows = [w for w in self.registry.windows if w.market_key == venue]
            result = self.registry.gate.evaluate_and_promote(
                challenger, venue_windows, champion
            )
            self.registry.last_result = {
                "passed": result.passed,
                "challenger_version_id": result.challenger_version_id,
                "champion_version_id": result.champion_version_id,
                "challenger_brier": result.challenger_brier,
                "champion_brier": result.champion_brier,
                "challenger_net_ev": result.challenger_net_ev,
                "champion_net_ev": result.champion_net_ev,
                "regime_win_rates": result.regime_win_rates,
                "jitter_retention": result.jitter_retention,
                "checks": result.checks,
                "reasons": result.reasons,
            }
            if result.passed:
                self.registry.champion_id = champion.version_id
                self.registry.versions[champion.version_id] = champion
            elif result.checks.get("sample_floor") is not False:
                # Enough OOS data and the challenger failed a hard gate — keep champion.
                apply_update = False
                logger.info(
                    "gate rejected model update (%s %ss): %s", venue, tf, result.reasons
                )

        if apply_update:
            ens.update(features, outcome, tf)
        ens.retrain_lgbm()
        self._persist_registry()

    def _persist_registry(self) -> None:
        """Best-effort persist the registry to SQLite (throttled, never crashes)."""
        now = time.time()
        if now - getattr(self, "_last_registry_save", 0.0) < 60.0:
            return
        self._last_registry_save = now
        try:
            from btc_edge.db.session import Database
            from btc_edge.ml.strategy_registry import save_registry

            db = Database(self.settings.db_path)
            save_registry(db.session(), self.registry)
            db.close()
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("registry persist failed: %s", exc)

    def _settle_positions(self, tf: int, venue: str, outcome: float) -> float:
        pnl = 0.0
        remaining: list[dict[str, Any]] = []
        for pos in self._open:
            if pos.get("tf") != tf or pos.get("venue") != venue:
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
            m["losses"] += 1 if trade_pnl < 0 else 0
            m["pnl_usd"] += trade_pnl
            if m["trades"]:
                m["win_rate"] = m["wins"] / m["trades"]
            vs = self._venue_stats[venue]
            vs["wins"] += 1 if trade_pnl > 0 else 0
            vs["losses"] += 1 if trade_pnl < 0 else 0
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
        self._venue_stats[venue]["pnl_usd"] += pnl
        self.safety.record_pnl(pnl)
        return pnl

    def _build_context(self, tf: int, venue: str, label: str) -> MarketContext | None:
        spot = self.state.btc_price
        if spot is None:
            return None
        now = time.time()
        from btc_edge.connectors.poly_client import compute_window_ts

        window_ts = compute_window_ts(now, tf)
        w = self._windows[(tf, venue)]
        open_price = w["open_price"] if w["open_price"] else spot
        time_remaining = self.state.window_countdowns.get(label, float(tf))

        if venue == "kalshi":
            kal = self.state.kalshi_market or {}
            contract_price = float(kal.get("prob_up") or 0.5)
            # KXBTC15M is a pure up/down binary (no strike): the reference price
            # is the spot at window open, and time_remaining is until close.
            close_ts = kal.get("close_time")
            if close_ts is not None:
                time_remaining = max(0.0, float(close_ts) - now)
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
        prob = self._ensembles[(tf, venue)].predict(feats, tf)
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

    async def _route_decisions(self, decisions: list[Any], tf: int, venue: str, label: str) -> None:
        pm = self.state.polymarket_markets.get(label, {})
        # Polymarket US uses one market slug for both sides (no per-outcome token IDs).
        pm_slug = pm.get("slug") or pm.get("up_token_id") or ""
        # Scale to the venue's LIVE wallet balance (the portfolio poller keeps
        # engine_state.wallet_balances current). Never trade on an unknown or drained
        # balance — sizing against a stale/unknown balance is exactly what let a
        # single cycle bet the whole account.
        live_balance = self.state.wallet_balances.get(venue)
        if live_balance is None or live_balance < 1.0:
            return
        # "Don't burn the bankroll": cap the total open notional for this venue at
        # max_exposure_pct% of the live balance. We track the notional of orders
        # already recorded this run (self._open) plus what we're about to place, so
        # the bot can never stack many minimum-size orders into a full-bankroll
        # position between balance polls.
        br = live_balance if live_balance > 0 else self.settings.trading.bankroll
        exposure_pct = self.settings.trading.max_exposure_pct
        if venue == "polymarket":
            exposure_pct = self.settings.trading.polymarket_exposure_pct
        max_exposure = br * exposure_pct / 100.0
        exposure = sum(p["price"] * p["size"] for p in self._open if p.get("venue") == venue)
        for d in decisions:
            size = self.router.size_for(d, venue=venue, bankroll=live_balance)
            if size <= 0:
                continue
            # No paper/simulated trading: only REAL exchange orders are recorded.
            # When live trading is disabled, we simply skip (nothing is faked).
            if not self.router.live_allowed:
                continue
            is_maker = d.order_type == "maker"
            fee = fee_models.fee(venue, size, d.price, is_maker)
            notional = d.price * size
            if exposure + notional > max_exposure:
                logger.info(
                    "exposure cap hit (%s): %.2f + %.2f > %.2f",
                    venue, exposure, notional, max_exposure,
                )
                continue
            token = pm_slug
            kal = self.state.kalshi_market or {}
            try:
                await self.router.place_order_async(
                    venue, d, d.price, size, is_maker,
                    token_id=token,
                    kalshi_ticker=kal.get("ticker") if venue == "kalshi" else None,
                )
            except Exception as exc:  # noqa: BLE001 - never crash the loop
                detail = getattr(getattr(exc, "response", None), "text", "") or str(exc)
                logger.warning("live order placement error (%s): %s", venue, detail)
                continue  # do not record failed placements

            # Record the REAL order that was just submitted to the exchange.
            exposure += notional
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
            vs = self._venue_stats[venue]
            vs["notional_usd"] += notional
            vs["trades"] += 1

            # Fire a throttled Telegram alert for the real fill.
            self._maybe_alert_order(venue, d, size, notional)

    def _maybe_alert_order(self, venue: str, d: Any, size: float, notional: float) -> None:
        """Send a throttled, non-blocking Telegram alert for a real order fill."""
        if not self._alerter.enabled:
            return
        now = time.time()
        if now - self._last_alert_ts < 30.0:
            return
        self._last_alert_ts = now
        text = (
            f"📈 BTC-Edge fill: {venue} {d.action} {d.side} {size:g} @ {d.price:.3f} "
            f"(${notional:.2f}, {d.strategy})"
        )
        with contextlib.suppress(Exception):
            asyncio.create_task(self._alerter.send(text))

    def _update_state(self, ctx: MarketContext, decisions: list[Any]) -> None:
        self.state.regime = ctx.regime
        # Per-strategy metrics + equity curve are the bot's *internal* accounting of
        # the real orders it placed. Venue totals / trade history / P&L come from
        # the REAL portfolio poller (portfolio_poller.py), not local settlement.
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

    def _filter_entries(self, decisions: list[Any], ctx: MarketContext, tf: int) -> list[Any]:
        """Drop entries that chase an already-moved side or fire too early.

        - Warmup: stay flat for the first ``entry_warmup_seconds`` of the window
          (no information to act on yet).
        - Anti-chase: skip "up" bets once BTC is already up ``chase_guard_pct``%
          since open (and symmetrically for "down"), so the bot never buys a move
          that has already happened.
        """
        chase = self.settings.trading.chase_guard_pct
        warmup = self.settings.trading.entry_warmup_seconds
        delta = ctx.window_delta_pct
        remaining = ctx.time_remaining
        kept: list[Any] = []
        for d in decisions:
            if remaining > tf - warmup:
                continue
            if d.side == "up" and delta > chase:
                continue
            if d.side == "down" and delta < -chase:
                continue
            kept.append(d)
        return kept

    def _flip_reversals(self, ctx: MarketContext, venue: str, decisions: list[Any]) -> None:
        """Exit-and-reverse when the ensemble signal flips mid-window.

        Tracks the last signal direction per venue. When it flips (up <-> down) we
        emit a taker decision on the NEW side immediately, so money isn't held on
        the stale side for the rest of the window. The router then buys the new
        side at the market, which (combined with settlement of the old side) nets
        out to a reversal.
        """
        direction = "up" if ctx.prob_estimate >= 0.5 else "down"
        prev = self._last_signal_dir.get(venue)
        self._last_signal_dir[venue] = direction
        if prev is None or prev == direction:
            return
        from btc_edge.strategies.base import StrategyDecision

        price = ctx.contract_price
        decisions.append(StrategyDecision(
            strategy="flip_reversal",
            side=direction,
            action="buy",
            price=price,
            size=5.0,
            order_type="taker",
            reason=f"signal flipped {prev} -> {direction}",
            prob_estimate=ctx.prob_estimate,
            ev_estimate=abs(ctx.prob_estimate - price),
            rule_snapshot={},
        ))

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
            # Expose our model's own up-probability per venue (the "ahead" reading).
            self.state.model_prob[venue] = ctx.prob_estimate
            decisions: list[Any] = []
            for strat in self.strategies:
                try:
                    decisions.extend(strat.evaluate(ctx))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("%s evaluate error: %s", strat.name, exc)
            self._flip_reversals(ctx, venue, decisions)
            decisions = self._filter_entries(decisions, ctx, tf)
            if decisions and not self.state.kill_switch:
                await self._route_decisions(decisions, tf, venue, label)
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
                self._maybe_notify_last_minute(time.time())
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                logger.warning("Trading loop error: %s", exc)
            await asyncio.sleep(interval)

    def _maybe_notify_last_minute(self, now: float) -> None:
        """Send a Telegram heads-up in the final minute of each 15m window.

        Fires once per (window_end, venue) so both Kalshi and Polymarket get a
        "closing soon" notification at T-60s with the current BTC price, the open
        reference, and the model's up/down call.
        """
        if not self._alerter.enabled:
            return
        spot = self.state.btc_price
        for tf, venue, label in TRADING_WINDOWS:
            window_end = (int(now) // tf + 1) * tf
            remaining = window_end - now
            if remaining <= 0 or remaining > 60:
                continue
            key = (window_end, venue)
            if key in self._notified_last_minute:
                continue
            self._notified_last_minute.add(key)

            if venue == "kalshi":
                prob = self.state.kalshi_market.get("prob_up")
                strike = self.state.open_strike
            else:
                pm = self.state.polymarket_markets.get(label, {})
                prob = pm.get("prob_up") or pm.get("up")
                strike = pm.get("open_price") or self.state.open_strike

            direction = "UP" if (prob is not None and prob >= 0.5) else "DOWN"
            prob_txt = f"{prob:.0%}" if isinstance(prob, (int, float)) else "--"
            price_txt = f"${spot:,.0f}" if isinstance(spot, (int, float)) else "--"
            strike_txt = f"${strike:,.0f}" if isinstance(strike, (int, float)) else "--"
            text = (
                f"⏰ {venue.upper()} 15m closing in {int(remaining)}s\n"
                f"BTC {price_txt} (open {strike_txt}) → {direction}\n"
                f"Model P(up) = {prob_txt}"
            )
            with contextlib.suppress(Exception):
                asyncio.create_task(self._alerter.send(text))

    async def seed_from_history(self) -> None:
        """Best-effort: pre-train both venue ensembles from real resolved markets.

        Called once at startup so neither model starts blank. Kalshi is warm-started
        on real settled windows (BTC move + outcome); Polymarket is warm-started on
        the resolved up/down base rate from the Gamma API.
        """
        from btc_edge.ml.seed_history import (
            seed_ensemble_from_kalshi,
            seed_ensemble_from_polymarket,
        )

        if self.kalshi_client is not None:
            ens = self._ensembles.get((900, "kalshi"))
            if ens is not None:
                await seed_ensemble_from_kalshi(ens, self.kalshi_client, 900)

        ens = self._ensembles.get((900, "polymarket"))
        if ens is not None:
            await seed_ensemble_from_polymarket(
                ens, self.settings.polymarket.gamma_api_url, 900
            )
