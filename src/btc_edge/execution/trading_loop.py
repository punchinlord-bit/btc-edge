"""Real-time trading loop: features → strategies → router → live settlement.

Ties the live market feed into the four strategies. Each tick it computes
features (regime, ``N(d2)`` implied probability, window delta, book imbalance,
micro-acceleration), runs the ensemble for ``prob_estimate``, evaluates the
strategies, routes decisions through the live router (Quarter-Kelly
sizing), settles closed windows, and mirrors results (signals, per-strategy
metrics, equity curve, regime, at-risk) into the dashboard state.

Live-only: orders route to the exchange when ``ALLOW_LIVE_TRADING == "TRUE"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import Any

from btc_edge.backtest import fee_models
from btc_edge.config import Settings
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.execution.risk_gate import RiskController
from btc_edge.execution.router import Router
from btc_edge.execution.safety import SafetyWatchdog
from btc_edge.features import indicators
from btc_edge.features.regime import RegimeClassifier, realized_vol_from_prices
from btc_edge.features.time_decay_gate import PreGraderGate
from btc_edge.ml.adversarial_grader import AdversarialGrader, build_adversarial_grader
from btc_edge.ml.calibration import twap_aware_prob
from btc_edge.ml.model import ProbabilityEnsemble
from btc_edge.ml.ollama_gate import OllamaGate, build_ollama_gate, compute_signal_state
from btc_edge.ml.shadow_ledger import Brain, ShadowLedger
from btc_edge.ml.strategy_registry import Registry
from btc_edge.strategies.base import MarketContext
from btc_edge.strategies.crowd_follow import CrowdFollowStrategy
from btc_edge.strategies.iv_mispricing import IvMispricingStrategy
from btc_edge.strategies.late_scalp import LateScalpStrategy
from btc_edge.strategies.market_maker import MarketMakerStrategy
from btc_edge.web.app import EngineState, get_ops_monitor
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
FEED_SAMPLE_HZ = 1.0  # live feed polls ~every 1s (finer-grained features)

# Passive/market-making strategies that only belong on Kalshi. Polymarket follows
# the disciplined directional triggers (MOMENTUM_BREAK + flip_reversal) instead of
# resting stale maker quotes that fire recklessly at candle rollovers.
_POLYMARKET_DISABLED = frozenset({
    "market_maker", "EARLY_SCALP", "LATE_SNIPE", "late_scalp", "crowd_follow", "iv_mispricing",
})

# Swarm consensus gate (Phase 4): entries require a strong, low-dispersion
# consensus; a collapse in quorum or a flip to negative edge triggers a scratch.
SWARM_MIN_QUORUM = 0           # of 20 agents must have after-fee edge (0 = disabled)
SWARM_MAX_DISPERSION = 1.0     # P75-P25 must be reasonably tight (1.0 = disabled)
SWARM_EXIT_QUORUM = 7          # drop below this while holding -> exit
SWARM_MIN_NET_EDGE = 0.01      # consensus edge must clear this to enter

# Weight given to the DeepSeek oracle's independent p_up when blending it into the
# ensemble's prob_estimate. Small by design: the LLM informs, it doesn't dominate.
DEEPSEEK_BLEND_WEIGHT = 0.15

# Celebration sound played on Telegram + the dashboard when the bot closes a
# position at a profit ("yeaaa"). Relative to the engine's working directory
# (the repo root when launched via scripts/run_engine.py).
WIN_AUDIO_PATH = "yeaaa_yeaaa.mp3"


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
        brain: Any | None = None,
    ) -> None:
        self.state = state
        self.settings = settings
        self.feed = feed
        self.registry = registry or Registry()
        self.kalshi_client = kalshi_client
        self.brain = brain
        self._ensembles: dict[tuple[int, str], ProbabilityEnsemble] = {
            (tf, venue): ProbabilityEnsemble() for tf, venue, _ in TRADING_WINDOWS
        }
        # The 20-agent swarm "Brain": dynamic-shrinkage consensus (shadow signal
        # exposed via state.swarm for telemetry; learning runs on every settlement).
        from btc_edge.brain.swarm import Swarm

        self.swarm = Swarm(seed=0)
        self.regime_cls = RegimeClassifier(
            rv_iv_threshold=settings.features.vol_rv_iv_threshold,
            funding_zscore_threshold=settings.features.funding_zscore_threshold,
        )
        self.router = Router(settings, kalshi_client=kalshi_client, poly_client=poly_client)
        self.safety = SafetyWatchdog(
            bankroll=settings.trading.bankroll,
            max_daily_loss_pct=settings.trading.max_daily_loss_pct,
        )
        # Persisted out-of-band kill switch: re-read from disk every tick so an arm
        # from ANY external process (Telegram bot, cron, manual file edit) halts
        # trading even if the dashboard/process is unreachable.
        self._killswitch = KillSwitch(settings.safety.kill_switch_file)
        self.strategies = self._build_strategies(settings)
        # Deterministic time-decay gate: blocks early-window coinflips and the
        # settlement freeze, only letting entries through with a real theta edge.
        self._pre_gate = PreGraderGate()
        # Venue risk gate (price cap + fee-adjusted net edge) and the shadow
        # paper-trading ledger that keeps training the model when a venue is $0.
        self._risk_gate = RiskController(
            certainty_all_in_pct=settings.trading.certainty_all_in_pct / 100.0,
            polymarket_max_contract_price=settings.trading.polymarket_max_contract_price,
        )
        self._shadow_ledger = ShadowLedger()
        self._shadow_brain = Brain(self._shadow_ledger)
        # Adversarial grader (DeepSeek-only until OpenAI keys are added). Used to
        # veto buy entries after the deterministic gates pass.
        self._grader: AdversarialGrader | None = build_adversarial_grader()
        if not self._grader.configured:
            self._grader = None
        # Local Ollama execution gate (fast, on-premise). Returns Y/N/H to
        # confirm the direction of a BUY entry; fails safe to Hold.
        self._ollama: OllamaGate | None = build_ollama_gate(settings)

        # Out-of-band Telegram alerts for real order fills (throttled to avoid spam).
        from btc_edge.execution.alerts import build_alerter

        self._alerter = build_alerter(settings)
        self._last_alert_ts = 0.0
        # Start the digest timer at "now" so a restart does NOT re-fire the daily
        # digest immediately (it only fires after a genuine 24h has elapsed).
        self._last_digest_ts = time.time()
        # Track which (window_start, alert_type) we've already sent a heads-up for,
        # so each 15m window fires exactly one notification — idempotent even
        # across loop resets or WebSocket reconnects.
        self._notified_last_minute: set[tuple[int, str]] = set()
        # Last ensemble signal direction per venue (for flip detection).
        self._last_signal_dir: dict[str, str] = {}
        self._master_bias: str = "NEUTRAL"
        self._last_edge_diag_ts: dict[str, float] = {}

        self._windows: dict[tuple[int, str], dict[str, Any]] = {
            (tf, venue): {"window_ts": None, "open_price": None, "last_features": {}}
            for tf, venue, _ in TRADING_WINDOWS
        }
        self._open: list[dict[str, Any]] = []
        # Idempotency guard: keys of orders already placed this window, so the
        # market-maker's periodic re-quotes don't stack duplicate orders.
        self._placed_order_keys: set[str] = set()
        # Shadow paper-trades already logged this window (dedup so a $0 venue doesn't
        # re-log the same hypothetical entry every tick).
        self._shadow_logged: set[str] = set()
        # Venues that just rolled over this tick (so tick() cancels stale resting
        # orders, e.g. Polymarket's open maker quotes from the previous window).
        self._rollover_venues: set[str] = set()
        self._total_pnl = 0.0
        self._equity_curve: list[tuple[float, float]] = []
        # Command Deck heartbeat counters (only increase on REAL work).
        self._ops_tick_count = 0
        self._ops_graded_rounds = 0
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

    @property
    def ollama_gate(self) -> OllamaGate | None:
        """The shared Ollama gate instance (used by the pre-warm/heartbeat loop)."""
        return self._ollama

    def _metric(self, name: str) -> dict[str, Any]:
        """Return (creating if needed) the per-strategy metrics dict.

        Internal strategy names (``take_profit``, ``flip_reversal``,
        ``position_manager``) aren't registered in ``self.strategies``, so they
        are created lazily here instead of raising ``KeyError`` when the first
        order for them is recorded.
        """
        m = self._metrics.get(name)
        if m is None:
            m = {
                "net_ev": 0.0,
                "win_rate": 0.0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "notional_usd": 0.0,
                "pnl_usd": 0.0,
                "fees_usd": 0.0,
            }
            self._metrics[name] = m
        return m

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
                min_edge=c.min_edge,
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
        # Three tagged intra-window strategies (always on).
        from btc_edge.strategies.intra_window import (
            EarlyScalpStrategy,
            LateSnipeStrategy,
            MomentumBreakStrategy,
        )

        out.append(EarlyScalpStrategy())
        out.append(MomentumBreakStrategy())
        out.append(LateSnipeStrategy())
        return out

    async def _cancel_stale_orders(self) -> None:
        """Cancel stale resting orders for venues that just rolled over.

        A 15m window rollover leaves last-minute maker/limit orders from the prior
        candle resting on the book; clearing them prevents the bot from carrying a
        stale bias into the new window.
        """
        if not self._rollover_venues:
            return
        venues = self._rollover_venues
        self._rollover_venues = set()
        if "polymarket" in venues and self.router.poly_client is not None:
            try:
                await self.router.poly_client.cancel_all()
                logger.info("polymarket: cancelled stale resting orders on rollover")
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning("polymarket cancel_all failed: %s", exc)

    def _sync_window(self, now: float) -> None:
        """Detect rollover for each trading window and settle the expired one."""
        from btc_edge.connectors.poly_client import compute_window_ts
        from btc_edge.features.indicators import time_weighted_average

        spot = self.state.btc_price
        # The Polymarket/Kalshi BTC up/down markets settle on a 60s TWAP reference
        # (Chainlink ``btc-usd-twap-60s``), so anchor the window open price to the
        # trailing 60s TWAP rather than a single instantaneous spot print.
        ref = time_weighted_average(self.state.price_history, 60.0, now) or spot
        for tf, venue, _ in TRADING_WINDOWS:
            w = self._windows[(tf, venue)]
            window_ts = compute_window_ts(now, tf)
            if w["window_ts"] is None:
                w["window_ts"] = window_ts
                w["open_price"] = ref
                if ref is not None:
                    self.state.open_strike = ref
                continue
            if window_ts != w["window_ts"]:
                self._settle_window(tf, venue)
                w["window_ts"] = window_ts
                w["open_price"] = ref
                # New window -> allow fresh (non-duplicate) orders again.
                self._placed_order_keys.clear()
                self._shadow_logged.clear()
                # Flag the rollover so tick() can cancel stale resting orders.
                self._rollover_venues.add(venue)
                # Expose the new window-open reference (60s TWAP) to the dashboard/
                # rocket widgets so their strike line matches the venue's window.
                if ref is not None:
                    self.state.open_strike = ref

    def _settle_window(self, tf: int, venue: str) -> None:
        w = self._windows[(tf, venue)]
        spot = self.state.btc_price
        from btc_edge.features.indicators import time_weighted_average

        # Settle against the 60s TWAP reference (matching the market's oracle),
        # not the instantaneous spot, so the recorded outcome aligns with how the
        # venue actually resolves the window.
        ref = time_weighted_average(self.state.price_history, 60.0, time.time()) or spot
        open_price = w["open_price"]
        if ref is None or open_price is None or open_price <= 0:
            self._open = [p for p in self._open if not (p.get("tf") == tf and p.get("venue") == venue)]
            return
        outcome = 1.0 if ref >= open_price else 0.0
        pnl = self._settle_positions(tf, venue, outcome)
        # Command Deck: the grader finished a real settlement pass.
        self._ops_graded_rounds += 1
        self._ops_beat("grader", self._ops_graded_rounds)
        # Grade + retrain the SHADOW paper-trades for this window (Brier + Hedge).
        self._shadow_brain.settle(f"{venue}-{tf}-{w.get('window_ts')}", int(outcome))
        if w["last_features"]:
            ens = self._ensembles[(tf, venue)]
            self._record_and_gate(ens, tf, venue, w["last_features"], outcome, open_price, ref)
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
        """Record the resolved window and update the model directly (no gate)."""
        from btc_edge.ml.model import feature_dict
        from btc_edge.ml.promotion_gate import WindowEvaluation, classify_3regime

        normalized = feature_dict(features, tf)
        abs_move_pct = abs(spot - open_price) / open_price * 100.0
        regime = classify_3regime(abs_move_pct)

        # Record the resolved window to the OOS ledger (the "brain").
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

        # Direct online learning — no champion/challenger gate.
        ens.update(features, outcome, tf)
        ens.retrain_lgbm()
        # Shadow-update the 20-agent swarm on the resolved window (online SGD +
        # Brier EMA) so its consensus keeps learning alongside the ensemble.
        self.swarm.update(self._swarm_feats(features, tf), outcome)
        self._persist_registry()
        # Command Deck: the brain finished a real learning pass.
        self._ops_beat("brain", len(ens._history_y))
        logger.info(
            "brain learn %s %ds: outcome=%s regime=%s samples=%d",
            venue, tf, "up" if outcome >= 0.5 else "down", regime, len(ens._history_y),
        )

        # Persist the resolved window to the SQLite brain ledger.
        if self.brain is not None:
            ws = (int(time.time()) // tf) * tf
            updown = "up" if outcome >= 0.5 else "down"
            if venue == "kalshi":
                self.brain.record_outcome({
                    "window_start_ts": ws, "regime": regime,
                    "kalshi_strike": open_price, "kalshi_close": spot, "kalshi_outcome": updown,
                })
            else:
                self.brain.record_outcome({
                    "window_start_ts": ws, "regime": regime,
                    "poly_strike": open_price, "poly_close": spot, "poly_outcome": updown,
                })

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
            self.state.last_settled_bet = {
                "id": f"{venue}-{pos['side']}-{int(time.time())}",
                "t": time.time(),
                "venue": venue,
                "dir": pos.get("side", "up"),
                "cents": round(float(pos.get("price", 0.5)) * 100),
                "size": pos.get("size", 0.0),
                "won": trade_pnl > 0,
                "pnlUsd": trade_pnl,
                "strategy": pos.get("strategy", ""),
                "dollarCost": round(float(pos.get("price", 0.5)) * pos.get("size", 0.0), 2),
                "exitType": pos.get("exit_type", "SETTLED"),
            }
            # Clean win/loss Telegram ping (throttled) so the operator sees the result.
            self._maybe_alert_settlement(pos, trade_pnl)
            if self.brain is not None and pos.get("trade_id"):
                self.brain.record_trade({
                    "trade_id": pos["trade_id"],
                    "venue": venue,
                    "window_start_ts": (int(time.time()) // tf) * tf,
                    "placed_at": pos.get("ts", time.time()),
                    "dir": pos.get("side", "up"),
                    "entry_cents": round(float(pos.get("price", 0.5)) * 100),
                    "contracts": pos.get("size", 0.0),
                    "btc_price_at_entry": pos.get("btc_price"),
                    "status": "settled",
                    "settled_pnl_usd": trade_pnl,
                    "won": 1 if trade_pnl > 0 else 0,
                })
            m = self._metric(pos["strategy"])
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
            self._append_trade_log({
                "type": "settle", "ts": time.time(), "tf": tf,
                "venue": pos.get("venue", ""), "strategy": pos["strategy"],
                "side": pos["side"], "action": pos["action"], "price": pos["price"],
                "size": pos["size"], "notional": pos.get("notional"),
                "fee": pos["fee"], "pnl": trade_pnl,
            })
        self._open = remaining
        self._recent_trades = self._recent_trades[-50:]
        self._venue_stats[venue]["pnl_usd"] += pnl
        self.safety.record_pnl(pnl)
        return pnl

    def _micro_acceleration(self, now: float) -> float:
        """1-minute candle delta acceleration from the live price history.

        Buckets ``(ts, price)`` samples into 60s candles, computes close-to-close
        returns, and returns the acceleration (last delta - previous delta) — the
        same ``indicators.micro_acceleration`` signal the backtest uses. Returns 0
        when there isn't enough history yet.
        """
        samples = self.state.price_history
        if len(samples) < 3:
            return 0.0
        closes_by_bucket: dict[int, float] = {}
        for ts, p in samples:
            if p is None:
                continue
            try:
                closes_by_bucket[int(float(ts) // 60.0)] = float(p)
            except (TypeError, ValueError):
                continue
        closes = [closes_by_bucket[k] for k in sorted(closes_by_bucket)]
        deltas: list[float] = []
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            if prev > 0:
                deltas.append((closes[i] - prev) / prev)
        return indicators.micro_acceleration(deltas)

    def _tape_feats(self) -> dict:
        """Short-horizon tape features derived from the 1s price buffer.

        We don't yet run a true 100ms tape, but the 1s buffer still exposes the
        short-horizon velocity + realized vol the swarm's Tape Sniper archetype
        needs, instead of leaving those inputs flat at 0 (which made the Tape
        Snipers dead weight). Velocity is percent-per-second; realized vol is
        annualized to match ``realized_vol_60s``.
        """
        prices = [p for _, p in self.state.price_history if p is not None]
        if len(prices) < 3:
            return {"micro_velocity_500ms": 0.0, "micro_velocity_2s": 0.0, "realized_vol_10s": 0.0}
        last, prev, anchor = prices[-1], prices[-2], prices[-3]
        v500 = (last - prev) / prev * 100.0 if prev > 0 else 0.0
        v2s = (last - anchor) / anchor * 100.0 if anchor > 0 else 0.0
        rv10 = realized_vol_from_prices(prices[-10:], sample_hz=FEED_SAMPLE_HZ)
        return {"micro_velocity_500ms": v500, "micro_velocity_2s": v2s, "realized_vol_10s": rv10}

    def _swarm_feats(self, feats: dict, tf: int, spread_cents: float = 2.0) -> dict:
        """Map the trading-loop feature dict to the swarm's feature set.

        Short-horizon tape features (micro_velocity_*, realized_vol_10s) are
        derived from the 1s price buffer via :meth:`_tape_feats` so the Tape
        Sniper archetype receives real signal at the current feed cadence.
        """
        tape = self._tape_feats()
        return {
            "micro_velocity_500ms": tape["micro_velocity_500ms"],
            "micro_velocity_2s": tape["micro_velocity_2s"],
            "micro_acceleration": float(feats.get("micro_acceleration", 0.0)),
            "realized_vol_10s": tape["realized_vol_10s"],
            "realized_vol_60s": float(feats.get("realized_vol", 0.0)),
            "strike_distance_zscore": float(feats.get("window_delta_pct", 0.0)) / 10.0,
            "orderbook_imbalance": float(feats.get("book_imbalance", 0.0)),
            "spread_cents": spread_cents,
            "time_remaining_norm": float(feats.get("time_remaining", 0.0)) / max(tf, 1),
            "news_sentiment": float(feats.get("news_sentiment", 0.0)),
        }

    def _build_context(self, tf: int, venue: str, label: str) -> MarketContext | None:
        spot = self.state.btc_price
        if spot is None:
            return None
        now = time.time()
        from btc_edge.connectors.poly_client import compute_window_ts
        from btc_edge.features.indicators import time_weighted_average

        window_ts = compute_window_ts(now, tf)
        w = self._windows[(tf, venue)]
        open_price = w["open_price"] if w["open_price"] else spot
        # Reference proxy = trailing 60s TWAP (the market's oracle is a 60s TWAP).
        ref = time_weighted_average(self.state.price_history, 60.0, now) or spot
        time_remaining = self.state.window_countdowns.get(label, float(tf))

        if venue == "kalshi":
            kal = self.state.kalshi_market or {}
            contract_price = float(kal.get("prob_up") or 0.5)
            # KXBTC15M settles against ``floor_strike`` (BRTI at window open). Anchor
            # the open reference to it so ``window_delta_pct`` is correct immediately
            # (including right after a restart, instead of reading 0 for the first
            # partial window).
            fs = kal.get("floor_strike")
            if fs is not None:
                with contextlib.suppress(TypeError, ValueError):
                    open_price = float(fs)
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

        window_delta = indicators.window_delta_pct(ref, open_price)
        # Oracle-aligned probability (ported from V1 ``core_prob``): a heavy-tailed
        # Student-t4 CDF with TWAP-aware variance and REALIZED vol, instead of the
        # thin-tailed ``N(d2)`` with a constant implied vol. ``N(d2)`` on a 15m
        # horizon was far too confident in small moves (a ~0.15% move read ~71% "up"),
        # which drove the historical ~39% realized win rate on Kalshi. The t4 model
        # also accounts for the 60s-TWAP settlement oracle (the ``/3`` averaging term),
        # aligning the model with how the venue actually resolves each window.
        sig_sec = rvol / math.sqrt(indicators.SECONDS_PER_YEAR) if rvol > 0 else 0.0
        elapsed = max(0.0, tf - time_remaining)
        option_prob, _ = twap_aware_prob(ref, open_price, time_remaining, elapsed, sig_sec)
        book_imb = 0.0
        # Micro-acceleration: the model's momentum signal, computed from 1-minute
        # candle deltas of the live price history (acceleration = last delta - prev
        # delta). This was previously hardcoded to 0 in live (only the backtest had
        # it), so the live model was missing its fastest signal.
        micro = self._micro_acceleration(now)

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
        # Adaptive calibration: shrink the (overconfident, small-sample) ensemble
        # toward the venue's ORDER-BOOK mid (``contract_price``) — the actual price
        # the bot trades against — not the N(d2) spot-implied probability. N(d2)
        # with a 15m horizon is far too confident in small BTC moves (a 0.15% move
        # reads ~71% "up"), which made the bot bet ~60% "up" while reality settled
        # ~39% "up". Calibrating toward the book mid means the model only leans away
        # from the market when it has a *demonstrated* Brier edge over it.
        market_prob = max(0.01, min(0.99, contract_price))
        prob = self._ensembles[(tf, venue)].calibrate(prob, market_prob)
        # Blend in the DeepSeek oracle's independent p_up (small weight, only when a
        # cached value exists) so the LLM adds a non-correlated signal to the model.
        ds = self.state.deepseek_prob
        if ds is not None:
            prob = (1.0 - DEEPSEEK_BLEND_WEIGHT) * prob + DEEPSEEK_BLEND_WEIGHT * ds
        prob = max(0.01, min(0.99, prob))
        w["last_features"] = feats

        # Synthetic 2-cent book around the market mid (no live CLOB stream yet).
        spread = 0.02
        bid_price = max(0.01, contract_price - spread / 2.0)
        ask_price = min(0.99, contract_price + spread / 2.0)
        bids = [(bid_price, 5.0)]
        asks = [(ask_price, 5.0)]

        # Swarm shadow consensus (the Brain): map live features to the swarm's
        # sub-second feature set (sub-second tape features are absent at a 1s feed,
        # so they default to 0) and expose quorum/dispersion/consensus for the
        # dashboard and a future quorum execution gate.
        swarm_res = self.swarm.consensus(
            self._swarm_feats(feats, tf), market_prob, min_edge=0.01
        )
        if not isinstance(getattr(self.state, "swarm", None), dict):
            self.state.swarm = {}
        self.state.swarm[venue] = swarm_res

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
            brti_lead=(self.state.spot_prices.get("kalshi") or 0.0)
            - (self.state.spot_prices.get("polymarket") or 0.0),
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
        # SHADOW mode: a $0 (or below-minimum) venue still runs the full Brain and
        # logs paper trades (graded later via Brier) so the model keeps training,
        # but no real order is ever signed against an empty/dust balance.
        shadow_mode = live_balance is None or live_balance < self._risk_gate.min_bet_dollars
        if shadow_mode:
            self.state.bot_status[venue] = "SHADOW: $0 balance (paper trading)"
            if time.time() - getattr(self, "_last_nobal_ts", 0.0) > 30.0:
                self._last_nobal_ts = time.time()
                logger.info(
                    "SHADOW mode %s: no live balance (wallet_balances=%s) — paper-trading",
                    venue, live_balance,
                )
            # Do NOT return: fall through and log shadow trades below.
        # "Don't burn the bankroll": cap the total open notional for this venue at
        # max_exposure_pct% of the live balance. We track the notional of orders
        # already recorded this run (self._open) plus what we're about to place, so
        # the bot can never stack many minimum-size orders into a full-bankroll
        # position between balance polls.
        br = live_balance if live_balance > 0 else self.settings.trading.bankroll
        exposure_pct = self.settings.trading.max_exposure_pct
        if venue == "polymarket":
            exposure_pct = self.settings.trading.polymarket_exposure_pct
        # Gusto recovery (keyword-gated): let Polymarket commit the full balance to
        # aggressive recovery bets instead of the conservative per-trade cap.
        gusto = self.state.gusto_mode and venue == "polymarket"
        if gusto:
            exposure_pct = 100.0
        max_exposure = br * exposure_pct / 100.0
        exposure = sum(p["price"] * p["size"] for p in self._open if p.get("venue") == venue)
        window_ts = self._windows.get((tf, venue), {}).get("window_ts") or int(time.time() // tf) * tf
        market_id = f"{venue}-{tf}-{window_ts}"
        for d in decisions:
            # Venue risk gate: hard price cap + fee-adjusted net EV (BUY entries only).
            # Stops Polymarket buying 85-90c traps and Kalshi churning low-edge noise.
            if d.action == "buy":
                model_prob_yes = d.prob_estimate if d.side == "up" else (1.0 - d.prob_estimate)
                risk = self._risk_gate.evaluate_decision(
                    venue=venue, side=d.side, win_prob=d.prob_estimate,
                    entry_price=d.price, live_balance=(live_balance or 0.0),
                )
                shadow_key = f"{venue}|{d.side}|{d.price:.4f}"
                # SHADOW ($0 balance): paper-train the model, never sign a real order.
                if shadow_mode:
                    if shadow_key not in self._shadow_logged:
                        self._shadow_logged.add(shadow_key)
                        self._shadow_brain.record_decision(
                            market_id=market_id, venue=venue,
                            agent_signals={"momentumAgent": model_prob_yes,
                                           "orderflowAgent": model_prob_yes,
                                           "volatilityAgent": model_prob_yes},
                            decision=risk, model_prob_yes=model_prob_yes,
                        )
                    continue
                # Risk-gate filtered: log it so the brain still learns the outcome,
                # but never route a price-capped / low-edge trade to the exchange.
                if not risk.approved:
                    if shadow_key not in self._shadow_logged:
                        self._shadow_logged.add(shadow_key)
                        self._shadow_brain.record_decision(
                            market_id=market_id, venue=venue,
                            agent_signals={"momentumAgent": model_prob_yes,
                                           "orderflowAgent": model_prob_yes,
                                           "volatilityAgent": model_prob_yes},
                            decision=risk, model_prob_yes=model_prob_yes,
                        )
                    continue
            size = self.router.size_for(d, venue=venue, bankroll=live_balance, gusto=gusto)
            if size <= 0:
                continue
            # Idempotency: the market-maker re-quotes the same levels every 15s;
            # skip an order we already placed this window so it doesn't stack.
            key = f"{venue}|{d.side}|{d.action}|{d.price:.6f}"
            if key in self._placed_order_keys:
                continue
            # Anti-scale-in: only ONE entry per (venue, side) per window. Scaling
            # into a mid-candle probability spike (e.g. a phantom 73% "up") turns a
            # single losing bet into a blown position. Only closing (sell) orders
            # may run against an existing position.
            if d.action == "buy" and any(
                p.get("venue") == venue and p.get("side") == d.side and p.get("action") == "buy"
                for p in self._open
            ):
                logger.info(
                    "scale-in blocked (%s %s): already holding this side this window",
                    venue, d.side,
                )
                continue
            # Live-only: when the ALLOW_LIVE_TRADING switch is off, stand aside
            # (there is no simulation — we simply don't place the order).
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
            trade_id = f"{venue}-{d.side}-{d.action}-{int(time.time())}"
            if d.action == "sell":
                # Sell-to-close: this closes a long we already entered, so its P&L is
                # realized NOW (exit - entry), not at window settlement. Re-recording
                # the close as a new open "sell" position made _settle_positions treat
                # it as a fresh short and settle it against the outcome — flipping a
                # real win into a spurious "Lost $..." (bought DOWN, closed at profit,
                # then reported a loss when DOWN actually won).
                self._realize_close(
                    venue=venue, tf=tf, strategy=d.strategy, side=d.side,
                    price=d.price, size=size,
                    entry_price=float(getattr(d, "entry_price", None) or d.price),
                    entry_fee=float(getattr(d, "entry_fee", 0.0) or 0.0),
                    exit_fee=fee, btc_price=self.state.btc_price, trade_id=trade_id,
                )
                self._placed_order_keys.add(key)
                self.state.bot_status[venue] = f"CLOSED {d.side.upper()} @ {round(d.price * 100)}¢"
                continue
            exposure += notional
            # Optimistic balance deduction so the wallet widget doesn't sit frozen
            # until the next 30s poll — deduct the notional immediately.
            bal = self.state.wallet_balances.get(venue)
            if bal is not None:
                self.state.wallet_balances[venue] = max(0.0, bal - notional)
            self._open.append({
                "tf": tf, "venue": venue, "side": d.side, "action": d.action,
                "price": d.price, "size": size, "fee": fee,
                "strategy": d.strategy, "prob": d.prob_estimate,
                "notional": notional, "ts": time.time(),
                "btc_price": self.state.btc_price,
                "trade_id": trade_id,
            })
            self._placed_order_keys.add(key)
            self.state.bot_status[venue] = f"FIRING {d.side.upper()} @ {round(d.price * 100)}¢"
            if self.brain is not None:
                self.brain.record_trade({
                    "trade_id": trade_id,
                    "venue": venue,
                    "window_start_ts": (int(time.time()) // tf) * tf,
                    "placed_at": time.time(),
                    "dir": d.side,
                    "entry_cents": round(d.price * 100),
                    "contracts": size,
                    "btc_price_at_entry": self.state.btc_price,
                    "status": "filled",
                })
            self._append_trade_log({
                "type": "signal", "ts": time.time(), "tf": tf, "venue": venue,
                "strategy": d.strategy, "side": d.side, "action": d.action,
                "price": d.price, "size": size, "prob": d.prob_estimate,
                "notional": notional, "fee": fee,
            })
            m = self._metric(d.strategy)
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
        """Send a throttled, non-blocking Telegram alert when an order is placed."""
        if not self._alerter.enabled:
            return
        now = time.time()
        if now - self._last_alert_ts < 30.0:
            return
        self._last_alert_ts = now
        venue_label = "KALSHI" if venue == "kalshi" else "POLYMARKET"
        direction = "UP" if d.side == "up" else "DOWN"
        pct = int(round(d.price * 100))
        # Each contract pays $1 if it resolves correct, so the payout is the contract
        # count; net profit is payout minus the notional spent.
        payout = size
        profit = payout - notional
        text = (
            f"🎯 {venue_label} bought {direction}: {size:.1f} contracts @ {pct}¢ — "
            f"spending ${notional:.2f} to make ${payout:.2f} (+${profit:.2f}). 🚀"
        )
        with contextlib.suppress(Exception):
            asyncio.create_task(self._alerter.send(text))

    def _maybe_alert_settlement(self, pos: dict, pnl: float) -> None:
        """Clean win/loss Telegram ping when a position settles.

        On a win, also bumps ``state.sell_win_seq`` (so the dashboard plays the
        "yeaaa" sound) and pushes the celebration audio to Telegram.
        """
        direction = "UP" if pos.get("side") == "up" else "DOWN"
        pct = int(round(float(pos.get("price", 0.5)) * 100))
        if pnl > 0:
            # Bump the win counter regardless of Telegram, so the dashboard's
            # celebration sound fires even if the bot token is unset.
            self.state.sell_win_seq += 1
            text = f"✅ Won ${pnl:.2f} on {direction} (bought at {pct}%). Nice! 🎉"
        else:
            text = f"❌ Lost ${-pnl:.2f} on {direction} (bought at {pct}%). On to the next one."
        if not self._alerter.enabled:
            return
        with contextlib.suppress(Exception):
            asyncio.create_task(self._alerter.send(text))
        if pnl > 0:
            with contextlib.suppress(Exception):
                asyncio.create_task(self._alerter.send_audio(WIN_AUDIO_PATH, caption=text))

    def _realize_close(
        self,
        *,
        venue: str,
        tf: int,
        strategy: str,
        side: str,
        price: float,
        size: float,
        entry_price: float,
        entry_fee: float,
        exit_fee: float,
        btc_price: float | None,
        trade_id: str,
    ) -> float:
        """Realize the P&L of a sell-to-close and record it as a settled trade.

        A close already has its cash flow (bought at ``entry_price``, sold at
        ``price``), so — unlike ``_settle_positions`` — it never touches the window
        outcome. Returns the realized P&L.
        """
        trade_pnl = (price - entry_price) * size - entry_fee - exit_fee
        # Credit the sale proceeds back to the wallet (optimistic, mirroring the
        # buy-side debit) so the wallet widget doesn't sit frozen until the poll.
        bal = self.state.wallet_balances.get(venue)
        if bal is not None:
            self.state.wallet_balances[venue] = bal + price * size
        self._recent_trades.append({
            "ts": time.time(), "tf": tf, "venue": venue, "strategy": strategy,
            "side": side, "action": "sell", "price": price, "size": size,
            "notional": price * size, "fee": entry_fee + exit_fee, "pnl": trade_pnl,
        })
        self._append_trade_log({
            "type": "settle", "ts": time.time(), "tf": tf, "venue": venue,
            "strategy": strategy, "side": side, "action": "sell", "price": price,
            "size": size, "notional": price * size, "fee": entry_fee + exit_fee,
            "pnl": trade_pnl,
        })
        m = self._metric(strategy)
        m["trades"] += 1
        m["wins"] += 1 if trade_pnl > 0 else 0
        m["losses"] += 1 if trade_pnl < 0 else 0
        m["pnl_usd"] += trade_pnl
        m["fees_usd"] += entry_fee + exit_fee
        if m["trades"]:
            m["win_rate"] = m["wins"] / m["trades"]
        vs = self._venue_stats[venue]
        vs["wins"] += 1 if trade_pnl > 0 else 0
        vs["losses"] += 1 if trade_pnl < 0 else 0
        vs["notional_usd"] += price * size
        vs["trades"] += 1
        vs["pnl_usd"] += trade_pnl
        self._recent_trades = self._recent_trades[-50:]
        self._total_pnl += trade_pnl
        self._equity_curve.append((time.time(), self._total_pnl))
        if len(self._equity_curve) > 200:
            self._equity_curve = self._equity_curve[-200:]
        self.safety.record_pnl(trade_pnl)
        if self.brain is not None:
            self.brain.record_trade({
                "trade_id": trade_id,
                "venue": venue,
                "window_start_ts": (int(time.time()) // tf) * tf,
                "placed_at": time.time(),
                "dir": side,
                "entry_cents": round(entry_price * 100),
                "contracts": size,
                "btc_price_at_entry": btc_price,
                "status": "settled",
                "settled_pnl_usd": trade_pnl,
                "won": 1 if trade_pnl > 0 else 0,
            })
        self._maybe_alert_settlement({"side": side, "price": entry_price}, trade_pnl)
        return trade_pnl

    def _sync_bot_bets(self) -> None:
        """Mirror this run's open orders into ``engine_state.bot_bets``.

        Maps each open position to the shape the rocket widgets expect so a bet's
        horizontal entry line + callout pill render at the correct BTC price.
        """
        bets: list[dict[str, Any]] = []
        for p in self._open:
            venue = p.get("venue") or "kalshi"
            side = str(p.get("side") or "up").lower()
            ts = float(p.get("ts") or time.time())
            price = float(p.get("price") or 0.5)
            size = float(p.get("size") or 0.0)
            bet: dict[str, Any] = {
                "id": f"{venue}-{side}-{int(ts)}",
                "t": ts,
                "btcPrice": p.get("btc_price"),
                "dir": side,
                "cents": round(price * 100),
                "status": "filled",
                "venue": venue,
                "strategy": p.get("strategy", ""),
                "dollarCost": round(price * size, 2),
                "exitType": p.get("exit_type", "OPEN"),
            }
            if venue == "kalshi":
                bet["contracts"] = size
            else:
                bet["shares"] = size
            bets.append(bet)
        self.state.bot_bets = bets

    def _update_state(self, ctx: MarketContext, decisions: list[Any]) -> None:
        self.state.regime = ctx.regime
        self._sync_bot_bets()
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

        Gusto recovery mode bypasses both filters for Polymarket so the bot can
        fire immediately and on already-moved sides to claw a balance back.
        """
        if self.state.gusto_mode and getattr(ctx, "venue", "") == "polymarket":
            return decisions
        chase = self.settings.trading.chase_guard_pct
        warmup = self.settings.trading.entry_warmup_seconds
        delta = ctx.window_delta_pct
        remaining = ctx.time_remaining
        kept: list[Any] = []
        for d in decisions:
            # Swarm quorum gate (Phase 4): a BUY entry only fires when the 20-agent
            # swarm has a strong, low-dispersion consensus on that side (>=14/20
            # agents agree and the IQR is tight). This replaces the naive "any
            # strategy with a hair of edge fires" behavior with a robust quorum vote.
            if d.action == "buy":
                swarm = self.state.swarm.get(ctx.venue) or {}
                qkey = "quorum_up" if d.side == "up" else "quorum_down"
                quorum = int(swarm.get(qkey, 0) or 0)
                dispersion = float(swarm.get("dispersion_iqr", 1.0) or 1.0)
                if quorum < SWARM_MIN_QUORUM or dispersion > SWARM_MAX_DISPERSION:
                    continue
                # Deterministic time-decay gate: hard-block the early-window
                # coinflip (Phase 1) and settlement freeze (Phase 4), and only let
                # a buy through inside the theta-crush / late-pin phases when the
                # move is structurally clear of the strike with a real EV edge.
                gate = self._pre_gate.evaluate(
                    elapsed_seconds=max(0.0, tf - remaining),
                    spot_price=ctx.current_spot,
                    strike_price=ctx.window_open_price,
                    realized_vol_per_sec=self._realized_vol_per_sec(ctx),
                    market_ask=(ctx.best_ask if d.side == "up" else (1.0 - ctx.best_bid)),
                    swarm_consensus=float(swarm.get("consensus_prob", 0.5) or 0.5),
                    true_prob=(ctx.prob_estimate if d.side == "up" else (1.0 - ctx.prob_estimate)),
                    recent_ticks=[p for _, p in self.state.price_history[-5:] if p is not None],
                )
                if not gate.allow:
                    continue
            if remaining > tf - warmup:
                continue
            if d.side == "up" and delta > chase:
                continue
            if d.side == "down" and delta < -chase:
                continue
            kept.append(d)
        return kept

    def _realized_vol_per_sec(self, ctx: MarketContext) -> float:
        """Annualized realized vol -> per-second, for the time-decay gate's zScore."""
        rv = float(getattr(ctx, "realized_vol", 0.0) or 0.0)
        if rv <= 0:
            return 0.0
        return rv / (indicators.SECONDS_PER_YEAR ** 0.5)

    async def _grade_buys(
        self, decisions: list[Any], ctx: MarketContext, tf: int, venue: str
    ) -> list[Any]:
        """Run the LLM gates over surviving BUY entries.

        Each buy that already passed the deterministic gates (PreGraderGate + swarm +
        RiskController) is confirmed by (1) the local Ollama 7B direction gate and
        (2) the remote DeepSeek adversarial grader. Only entries that survive both
        are routed. Sells/exits are never gated (you can always flatten a position).
        """
        if self._grader is None and self._ollama is None:
            return decisions
        # Fast mode: LLM veto gates disabled -> the deterministic gates decide alone.
        if not getattr(self.settings.trading, "llm_gates", True):
            return decisions
        swarm = self.state.swarm.get(venue) or {}
        consensus = float(swarm.get("consensus_prob", 0.5) or 0.5)
        elapsed = max(0.0, tf - ctx.time_remaining)
        sigma_remaining = self._realized_vol_per_sec(ctx) * (max(0.0, tf - elapsed) ** 0.5)
        if sigma_remaining > 0:
            z_score = abs(ctx.current_spot - ctx.window_open_price) / (
                ctx.current_spot * sigma_remaining
            )
        else:
            z_score = 0.0

        # One Ollama prompt per window (same signal state + tape for every buy).
        ollama_input: str | None = None
        if self._ollama is not None:
            signal_state = compute_signal_state(ctx.prob_estimate, ctx.window_delta_pct)
            tape_note = (
                f"BTC {ctx.window_delta_pct:+.2f}% since open, {int(elapsed)}s in, "
                f"model P(up) {ctx.prob_estimate:.0%}"
            )
            ollama_input = self._ollama.build_input(signal_state, tape_note)

        kept: list[Any] = []
        for d in decisions:
            if d.action != "buy":
                kept.append(d)
                continue
            # 1. Local Ollama 7B direction gate (fast, ~400ms). "H" (Hold) and any
            # timeout/error are FAIL-OPEN — they mean "no strong opinion", so the
            # deterministic gates decide. Only a clear Y<->N contradiction vetoes.
            # (Previously "H" vetoed, which held every signal for hours.)
            if self._ollama is not None and ollama_input is not None:
                verdict = await self._ollama.chat(ollama_input)
                want = "Y" if d.side == "up" else "N"
                if verdict in ("Y", "N") and verdict != want:
                    self._ollama.note_veto()
                    logger.info("ollama veto (%s %s): got %s want %s", venue, d.side, verdict, want)
                    continue
            # 2. Remote DeepSeek adversarial grader (slower, 1600ms).
            if self._grader is not None:
                telemetry = self._grader.build_telemetry(
                    elapsed_seconds=elapsed,
                    z_score=z_score,
                    true_prob=d.prob_estimate,
                    market_ask=d.price,
                    ev_edge=d.prob_estimate - d.price,
                    order_book_imbalance=float(getattr(ctx, "book_imbalance", 0.0) or 0.0),
                    swarm_consensus=consensus,
                )
                try:
                    verdict = await self._grader.grade(
                        telemetry, z_score=z_score, swarm_consensus=consensus
                    )
                except Exception as exc:  # noqa: BLE001 - never crash the loop
                    logger.warning("grader error (%s): %s", venue, exc)
                    kept.append(d)  # fail-open on grader error
                    continue
                if verdict.approved:
                    kept.append(d)
                else:
                    logger.info("grader veto (%s %s): %s", venue, d.side, verdict.source)
            else:
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

    def _take_profit(self, ctx: MarketContext, venue: str, tf: int, decisions: list[Any]) -> None:
        """Close a position that is up >= take_profit_pct% and free capital to re-enter.

        Marks each open buy to market (up -> contract_price, down -> 1 - contract_price);
        once the gain vs entry exceeds the threshold we emit a sell-to-close and drop
        the position so the strategies can re-enter if there is still edge.
        """
        threshold = 1.0 + self.settings.trading.take_profit_pct / 100.0
        mark_up = ctx.contract_price
        for pos in list(self._open):
            if pos.get("venue") != venue or pos.get("tf") != tf:
                continue
            if pos.get("action") != "buy" or pos.get("price", 0.0) <= 0:
                continue
            mark = mark_up if pos["side"] == "up" else (1.0 - mark_up)
            if mark / pos["price"] < threshold:
                continue
            from btc_edge.strategies.base import StrategyDecision

            decisions.append(StrategyDecision(
                strategy="take_profit",
                side=pos["side"],
                action="sell",
                price=mark,
                size=pos["size"],
                order_type="taker",
                reason=f"take profit {round((mark / pos['price'] - 1.0) * 100.0)}%",
                prob_estimate=ctx.prob_estimate,
                ev_estimate=0.0,
                rule_snapshot={},
                entry_price=pos["price"],
                entry_fee=float(pos.get("fee", 0.0) or 0.0),
            ))
            self._open.remove(pos)

    def _comeback_prob(self, distance: float, remaining: float, sigma: float = 1.65) -> float:
        """One-tailed probability the held side comes back before settlement.

        ``distance`` is how far (dollars) BTC is from the strike on the side that
        hurts us; ``remaining`` is seconds left. Uses the spec's z-score form.
        """
        import math

        if remaining <= 0:
            return 0.0
        z = abs(distance) / (sigma * math.sqrt(remaining))
        return 0.5 * math.erfc(z / math.sqrt(2.0))

    def _manage_positions(self, ctx: MarketContext, venue: str, tf: int, decisions: list[Any]) -> None:
        """Active position manager: early profit scalp + 90c lock-in + crumbs bailout.

        Every tick checks each open buy against the live bid and exits early to lock
        profit or salvage crumbs before liquidity vanishes, instead of riding every
        binary blindly to settlement.
        """
        from btc_edge.strategies.base import StrategyDecision

        mid = ctx.contract_price
        remaining = ctx.time_remaining
        spot = ctx.current_spot
        strike = ctx.window_open_price
        for pos in list(self._open):
            if pos.get("venue") != venue or pos.get("tf") != tf:
                continue
            if pos.get("action") != "buy" or pos.get("price", 0.0) <= 0:
                continue
            side = pos.get("side", "up")
            # Synthetic 2-cent book: YES bid = mid - 1c; NO bid = 1 - (mid + 1c).
            best_bid = (mid - 0.01) if side == "up" else (1.0 - (mid + 0.01))
            best_bid = max(0.01, min(0.99, best_bid))
            entry = pos.get("price", 0.0)
            bid_cents = best_bid * 100.0
            entry_cents = entry * 100.0
            unrealized = (best_bid - entry) * pos.get("size", 0.0)
            # Swarm consensus quorum on THIS position's side (for the scratch exit).
            swarm = self.state.swarm.get(venue) or {}
            qkey = "quorum_up" if side == "up" else "quorum_down"
            swarm_quorum = int(swarm.get(qkey, SWARM_EXIT_QUORUM) or SWARM_EXIT_QUORUM)

            reason = None
            # 1. 90c+ lock-in: take near-max payout instead of risking settlement.
            if bid_cents >= 88.0:
                reason = f"90c lock-in {bid_cents:.0f}c"
            # 2. Quick scalp: +7c or +16% ROI with >90s left.
            elif (
                (bid_cents >= entry_cents + 7.0 or (entry > 0 and best_bid / entry >= 1.16))
                and remaining > 90
            ):
                reason = f"scalp +{bid_cents - entry_cents:.0f}c"
            # 3. Swarm scratch: consensus quorum on our side collapsed below the
            #    exit threshold -> exit before market makers re-price against us.
            elif swarm_quorum < SWARM_EXIT_QUORUM and remaining > 45:
                reason = f"swarm scratch q={swarm_quorum}"
            # 4. Hopeless: market is ~97%+ against us (our side's bid is a sliver of
            #    1-3c). Pull whatever crumbs are left at ANY remaining time instead
            #    of letting the position ride to $0 at settlement.
            elif unrealized < 0 and bid_cents <= 3.0:
                reason = f"hopeless salvage {bid_cents:.0f}c"
            # 5. Crumbs bailout: losing, low comeback prob, but bid still >= 6c.
            elif unrealized < 0 and 45.0 <= remaining <= 240.0 and bid_cents >= 6.0:
                distance = (strike - spot) if side == "up" else (spot - strike)
                if self._comeback_prob(distance, remaining) <= 0.16:
                    reason = f"crumbs salvage {bid_cents:.0f}c"

            if reason is None:
                continue
            exit_type = (
                "SALVAGED_CRUMBS"
                if reason.startswith("crumbs")
                or reason.startswith("hopeless")
                or reason.startswith("swarm")
                else "SCALP_WIN"
            )
            self.state.last_settled_bet = {
                "id": f"{venue}-{side}-{int(time.time())}",
                "t": time.time(),
                "venue": venue,
                "dir": side,
                "cents": round(entry * 100),
                "size": pos.get("size", 0.0),
                "won": unrealized > 0,
                "pnlUsd": unrealized,
                "strategy": pos.get("strategy", ""),
                "dollarCost": round(entry * pos.get("size", 0.0), 2),
                "exitType": exit_type,
            }
            decisions.append(StrategyDecision(
                strategy="position_manager",
                side=side,
                action="sell",
                price=best_bid,
                size=pos["size"],
                order_type="taker",
                reason=reason,
                prob_estimate=ctx.prob_estimate,
                ev_estimate=0.0,
                rule_snapshot={},
                entry_price=pos["price"],
                entry_fee=float(pos.get("fee", 0.0) or 0.0),
            ))
            self._open.remove(pos)
            logger.info("position manager %s %s: %s", venue, side, reason)

    def _compute_master_bias(self) -> str:
        """Single cross-venue directional thesis from the synthetic BRTI lead.

        The Kalshi synthetic BRTI leads Polymarket's Chainlink by ~1s, so a spread
        of >$1.5 in either direction is a real momentum signal. Returns UP / DOWN /
        NEUTRAL so both venues stop hedging against each other.
        """
        kalshi = self.state.spot_prices.get("kalshi")
        poly = self.state.spot_prices.get("polymarket")
        if kalshi is None or poly is None:
            return "NEUTRAL"
        spread = kalshi - poly
        if spread > 1.5:
            return "UP"
        if spread < -1.5:
            return "DOWN"
        return "NEUTRAL"

    def _maybe_disarm_gusto(self) -> None:
        """Auto-disarm "gusto" recovery mode after its timeout so it can't leak.

        The keyword only arms it; a timer always closes the window so an operator
        can't forget it on and drain the wallet unattended.
        """
        if not self.state.gusto_mode:
            return
        timeout = self.settings.gusto_mode_timeout_seconds
        if timeout > 0 and time.time() - self.state.gusto_armed_at >= timeout:
            self.state.gusto_mode = False
            logger.info("gusto mode auto-disarmed after %ds timeout", timeout)

    def _ops_beat(self, name: str, progress: int | None = None) -> None:
        """Send a Command Deck heartbeat (never raises, never blocks)."""
        with contextlib.suppress(Exception):
            get_ops_monitor().beat(name, {"progress": progress} if progress is not None else None)

    async def tick(self) -> None:
        if self.state.btc_price is None:
            return
        now = time.time()
        self._ops_tick_count += 1
        self._ops_beat("kalshi-exec", self._ops_tick_count)
        self._ops_beat("polymarket-exec", self._ops_tick_count)
        # Re-read the persisted kill switch from disk EVERY tick so an out-of-band
        # arm (Telegram bot, cron, manual file edit) halts new entries immediately,
        # independent of the in-memory state that's only synced at startup.
        self.state.kill_switch = self._killswitch.is_armed()
        self._maybe_disarm_gusto()
        self._sync_window(now)
        # Clear stale resting orders for any venue that just rolled over.
        await self._cancel_stale_orders()
        self._master_bias = self._compute_master_bias()
        all_decisions: list[Any] = []
        last_ctx: MarketContext | None = None
        for tf, venue, label in TRADING_WINDOWS:
            ctx = self._build_context(tf, venue, label)
            if ctx is None:
                continue
            last_ctx = ctx
            # Expose our model's own up-probability per venue (the "ahead" reading).
            self.state.model_prob[venue] = ctx.prob_estimate
            # Throttled diagnostic: log model prob vs book + edge side-by-side so a
            # scale mismatch (0-1 vs 0-100) or a None/0 book is visible in the logs.
            # Throttle is per-venue so BOTH kalshi and polymarket show up.
            if time.time() - self._last_edge_diag_ts.get(venue, 0.0) > 15.0:
                self._last_edge_diag_ts[venue] = time.time()
                logger.info(
                    "edge-diag %s: model_prob=%.4f bid=%.4f ask=%.4f edge=%.4f delta_pct=%.4f",
                    venue, ctx.prob_estimate, ctx.best_bid, ctx.best_ask,
                    ctx.prob_estimate - ctx.best_ask, ctx.window_delta_pct,
                )
            decisions: list[Any] = []
            for strat in self.strategies:
                # Polymarket discipline: drop passive/reckless entries (market_maker,
                # scalps, crowd/iv) and keep only the directional MOMENTUM_BREAK
                # trigger + flip_reversal, matching Kalshi's disciplined behavior.
                if venue == "polymarket" and strat.name in _POLYMARKET_DISABLED:
                    continue
                try:
                    decisions.extend(strat.evaluate(ctx))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("%s evaluate error: %s", strat.name, exc)
            self._take_profit(ctx, venue, tf, decisions)
            self._manage_positions(ctx, venue, tf, decisions)
            self._flip_reversals(ctx, venue, decisions)
            decisions = self._filter_entries(decisions, ctx, tf)
            # LLM gates (local Ollama 7B direction gate + remote DeepSeek adversarial
            # grader): veto buy entries that don't clear both. Runs for kalshi AND
            # polymarket.
            decisions = await self._grade_buys(decisions, ctx, tf, venue)
            if decisions and not self.state.kill_switch:
                await self._route_decisions(decisions, tf, venue, label)
            all_decisions.extend(decisions)

        if last_ctx is not None:
            self._update_state(last_ctx, all_decisions)

        # The kill switch is USER-ONLY: nothing in the engine may auto-halt. The
        # daily-loss circuit breaker is intentionally disabled so the bot never
        # stops itself — halting is always a deliberate dashboard action.
        self.safety.state.kill_switch = self.state.kill_switch

    def _append_trade_log(self, record: dict[str, Any]) -> None:
        """Append one signal/settlement record to ``data/trades.jsonl`` (best-effort).

        This is the permanent "brain" ledger of every bet the bot makes and its
        outcome, independent of the in-memory loops, so the learning process can
        be audited and replayed.
        """
        import json
        from pathlib import Path

        try:
            path = Path("data/trades.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - logging must never crash the loop
            logger.warning("trade log append failed: %s", exc)

    async def run(self, interval: float = 2.0) -> None:
        while True:
            try:
                await self.tick()
                self._maybe_notify_last_minute(time.time())
                self._maybe_send_daily_digest(time.time())
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                logger.warning("Trading loop error: %s", exc)
            await asyncio.sleep(interval)

    def _maybe_notify_last_minute(self, now: float) -> None:
        """Ping Telegram in the final minute ONLY when a position is actually at risk."""
        if not self._alerter.enabled:
            return
        tf = TRADING_WINDOWS[0][0]  # 900
        window_end = (int(now) // tf + 1) * tf
        remaining = window_end - now
        if remaining <= 0 or remaining > 60:
            return
        # Only bother the operator when there's a real position on the line.
        if not self._open:
            return
        window_start = (int(now) // tf) * tf
        key = (window_start, "closing_in")
        if key in self._notified_last_minute:
            return
        self._notified_last_minute.add(key)
        with contextlib.suppress(Exception):
            asyncio.create_task(self._alerter.send("⏰ Window about to settle — holding our position. 🤞"))

    def _maybe_send_daily_digest(self, now: float) -> None:
        """Send one clean daily recap (once per 24h) in plain, human terms."""
        if not self._alerter.enabled:
            return
        if now - self._last_digest_ts < 86400.0:
            return
        self._last_digest_ts = now

        wins = losses = 0
        pnl = 0.0
        for venue in ("kalshi", "polymarket"):
            vs = self.state.venue_stats.get(venue, {}) or {}
            wins += int(vs.get("wins", 0))
            losses += int(vs.get("losses", 0))
            pnl += float(vs.get("pnl_usd", 0.0))
        trades = wins + losses
        if trades == 0:
            text = "📊 Today: no trades — waiting for the right setup."
        else:
            tone = "Keep it up! 💪" if pnl >= 0 else "Better luck tomorrow. 🙏"
            text = f"📊 Today: {wins} wins, {losses} losses, {pnl:+.2f}. {tone}"
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
