"""BTC-Edge live engine entry point.

Loads configuration, initializes the database, wires connectors (when API keys
are present), starts the safety watchdog + the news-intelligence loop, and serves
the FastAPI dashboard. Live-only: orders route to the exchange when
``ALLOW_LIVE_TRADING == "TRUE"`` (paper mode removed).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any

from dotenv import load_dotenv

# ruff: noqa: E402  (load_dotenv must run before the btc_edge imports below)
load_dotenv()

from btc_edge.config import load_settings
from btc_edge.connectors.news_scraper import NewsScraper
from btc_edge.db.session import Database
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.execution.trading_loop import TradingLoop
from btc_edge.features.sentiment_analyzer import SentimentAnalyzer
from btc_edge.ml.self_healing_filter import NewsIntelligence, SelfHealingFilter
from btc_edge.web.app import _registry, app, engine_state, get_ops_monitor, set_ollama_gate, set_ops_probes
from btc_edge.web.live_feed import LiveMarketFeed


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _news_loop(settings, intelligence: NewsIntelligence) -> None:
    """Periodically fetch + score news and refresh the live dashboard state."""
    log = logging.getLogger("news_loop")
    refresh = max(10, settings.news.refresh_seconds)
    while True:
        try:
            items = await intelligence.scraper.fetch()
            signals = intelligence.analyze_items(items)
            aggregate = intelligence.filter.aggregate(
                [(s.source, s.sentiment, s.confidence) for s in signals]
            )
            engine_state.news_sentiment = aggregate
            engine_state.news_headlines = [
                {"source": i.source, "title": i.title} for i in items[:8]
            ]
            log.info("News loop: %d items, sentiment %+.3f", len(items), aggregate)
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            log.warning("News loop error: %s", exc)
        await asyncio.sleep(refresh)


_last_brain_report_ts = 0.0


async def _portfolio_loop(settings, engine_state, kalshi_client, poly_client, brain=None) -> None:
    """Periodically refresh REAL portfolio data (balances, fills, settlements,
    positions) from Kalshi + Polymarket and publish it to the dashboard."""
    global _last_brain_report_ts
    from btc_edge.execution.portfolio_poller import poll_kalshi, poll_polymarket

    log = logging.getLogger("portfolio_loop")
    # Kalshi fills/settlements are keyed by the SPECIFIC 15m market ticker
    # (e.g. KXBTC15M-26NOV021500-00), not the series ticker "KXBTC15M", so
    # filtering by the series returns nothing and the W/L scorecard reads 0/0.
    # Fetch ALL fills/settlements (this account only trades KXBTC15M anyway).
    ticker = None
    interval = max(5, int(getattr(settings, "portfolio_poll_seconds", 10) or 10))
    while True:
        recent: list = []
        open_pos: list = []
        totals = {
            "total_trades": 0.0,
            "total_notional_usd": 0.0,
            "total_pnl_usd": 0.0,
            "total_fees_usd": 0.0,
        }
        if kalshi_client is not None:
            try:
                k = await poll_kalshi(kalshi_client, ticker=ticker)
                engine_state.wallet_balances["kalshi"] = k.balance
                engine_state.venue_stats["kalshi"] = k.venue_stats
                engine_state.kalshi_auth_status = k.auth_status
                if brain is not None:
                    vs = k.venue_stats or {}
                    brain.upsert_rollup("kalshi", {
                        "balance_usd": k.balance,
                        "wins": vs.get("wins", 0),
                        "losses": vs.get("losses", 0),
                        "total_pnl_usd": vs.get("pnl_usd", 0.0),
                    })
                recent.extend(k.recent_trades)
                open_pos.extend(k.open_positions)
                for key in totals:
                    totals[key] += k.trade_totals.get(key, 0.0)
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.warning("Kalshi portfolio poll error: %s", exc)
        if poly_client is not None:
            try:
                p = await poll_polymarket(poly_client)
                engine_state.wallet_balances["polymarket"] = p.balance
                engine_state.venue_stats["polymarket"] = p.venue_stats
                if brain is not None:
                    vs = p.venue_stats or {}
                    brain.upsert_rollup("polymarket", {
                        "balance_usd": p.balance,
                        "wins": vs.get("wins", 0),
                        "losses": vs.get("losses", 0),
                        "total_pnl_usd": vs.get("pnl_usd", 0.0),
                    })
                recent.extend(p.recent_trades)
                open_pos.extend(p.open_positions)
                for key in totals:
                    totals[key] += p.trade_totals.get(key, 0.0)
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.warning("Polymarket portfolio poll error: %s", exc)

        if recent:
            recent.sort(key=lambda t: float(t.get("ts") or 0.0))
            engine_state.recent_trades = recent[-100:]
        if open_pos:
            engine_state.open_positions = open_pos[-50:]
        engine_state.trade_totals = totals
        # Periodic "brain report": surface the real W/L + P&L scorecard in the logs
        # (in addition to the dashboard) so the bot's record is always visible.
        if time.time() - _last_brain_report_ts >= 60.0:
            _last_brain_report_ts = time.time()
            parts = []
            for _v in ("kalshi", "polymarket"):
                _vs = engine_state.venue_stats.get(_v, {}) or {}
                _w = int(_vs.get("wins", 0) or 0)
                _l = int(_vs.get("losses", 0) or 0)
                _tot = _w + _l
                _wr = (_w / _tot) if _tot else 0.0
                parts.append(
                    f"{_v}: {_w}W/{_l}L ({_wr:.0%}) PnL=${_vs.get('pnl_usd', 0.0):+.2f}"
                )
            log.info("brain report: %s", "  |  ".join(parts))
        await asyncio.sleep(interval)


def _deepseek_snapshot(state) -> dict[str, Any]:
    """Build a compact live snapshot for the DeepSeek oracle from engine state."""
    from btc_edge.features.indicators import (
        option_implied_prob,
        time_weighted_average,
        window_delta_pct,
    )

    spot = state.btc_price
    if spot is None:
        return {}
    ref = time_weighted_average(state.price_history, 60.0, time.time()) or spot
    open_price = state.open_strike or spot
    remaining = max(0.0, float(state.window_countdowns.get("15m", 900.0)))
    implied = 0.5
    kal = state.kalshi_market or {}
    return {
        "btc_price": spot,
        "window_open_price": open_price,
        "window_delta_pct": window_delta_pct(ref, open_price),
        "option_implied_prob": option_implied_prob(ref, open_price, implied, remaining),
        "news_sentiment": float(state.news_sentiment or 0.0),
        "time_remaining": remaining,
        "regime": state.regime,
        "contract_price": float(kal.get("prob_up") or 0.5),
        "recent_prices": [p for _, p in state.price_history[-40:] if p is not None],
    }


async def _reconciler_loop(settings, engine_state, kalshi_client) -> None:
    """Periodically reconcile Kalshi AI vs human vs shadow and push a Telegram report."""
    import contextlib

    from btc_edge.execution.reconciler import (
        KalshiReconciler,
        ai_live_from_ledger,
        format_telegram,
        human_manual_from_file,
    )
    from btc_edge.ml.shadow_ledger import ShadowLedger
    from btc_edge.web.app import _alerter

    log = logging.getLogger("reconciler")
    reconciler = KalshiReconciler(kalshi_client) if kalshi_client else None
    shadow_ledger = ShadowLedger()
    interval = 60.0
    last_sent = 0.0
    last_signature: tuple | None = None
    while True:
        try:
            if reconciler is not None:
                shadow = shadow_ledger.state["metrics"].get("kalshi", {})
                ai_live = ai_live_from_ledger(venue="kalshi")
                human = human_manual_from_file()
                sb = await reconciler.reconcile(
                    ticker=settings.kalshi.market_ticker,
                    shadow_metrics=shadow,
                    ai_live_metrics=ai_live,
                    human_metrics=human,
                )
                engine_state.attribution = {
                    "ai_live": {"wins": sb.ai_live.wins, "losses": sb.ai_live.losses,
                                "pnl": sb.ai_live.pnl},
                    "ai_shadow": {"wins": sb.ai_shadow.wins, "losses": sb.ai_shadow.losses,
                                  "pnl": sb.ai_shadow.pnl},
                    "human_manual": {"wins": sb.human_manual.wins, "losses": sb.human_manual.losses,
                                     "pnl": sb.human_manual.pnl},
                }
                # Push to Telegram ONLY when the scoreboard actually changed (a new
                # trade/settlement landed) — never re-send the same "AI so far" line on
                # a timer. Also keep a 15-min floor so a burst of trades doesn't spam.
                signature = (
                    sb.ai_live.wins, sb.ai_live.losses, sb.ai_live.pnl,
                    sb.ai_shadow.wins, sb.ai_shadow.losses,
                    sb.human_manual.wins, sb.human_manual.losses,
                )
                if (
                    _alerter.enabled
                    and (time.time() - last_sent >= 900.0)
                    and signature != last_signature
                    and (sb.ai_live.trades or sb.ai_shadow.trades or sb.human_manual.trades)
                ):
                    last_sent = time.time()
                    last_signature = signature
                    with contextlib.suppress(Exception):
                        asyncio.create_task(_alerter.send(format_telegram(sb)))
                log.info("reconciler: AI %dW/%dL · Human %dW/%dL · Shadow %dW/%dL",
                         sb.ai_live.wins, sb.ai_live.losses,
                         sb.human_manual.wins, sb.human_manual.losses,
                         sb.ai_shadow.wins, sb.ai_shadow.losses)
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            log.warning("reconciler error: %s", exc)
        await asyncio.sleep(interval)


async def _deepseek_loop(settings, engine_state, oracle) -> None:
    """Periodically query DeepSeek for ``p_up`` and cache it into engine state."""
    log = logging.getLogger("deepseek_oracle")
    interval = 30.0
    while True:
        try:
            if oracle.configured:
                snap = _deepseek_snapshot(engine_state)
                if snap:
                    p = await oracle.query(snap)
                    if p is not None:
                        engine_state.deepseek_prob = p
                        engine_state.deepseek_reason = oracle.reason
                        log.info(
                            "DeepSeek oracle p_up=%.3f conf=%.2f (%s)",
                            p, oracle.latest_confidence or 0.0, oracle.reason,
                        )
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            log.warning("DeepSeek loop error: %s", exc)
        await asyncio.sleep(interval)


def _seconds_until_next_utc_run(hour: int = 0, minute: int = 5) -> float:
    """Seconds until the next daily run time (default 00:05 UTC)."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _nightly_analyzer_loop(settings, engine_state, registry) -> None:
    """Run the late-night analyzer once daily at 00:05 UTC.

    Reads the prior day's resolved windows, computes calibration + grid search,
    writes ``candidate_config.json``, and publishes a Telegram summary + dashboard
    card. The candidate file is always written for review; ``auto_apply`` is
    surfaced in the report (and honored only by an operator toggle).
    """
    import contextlib

    log = logging.getLogger("nightly_analyzer")
    while True:
        try:
            delay = _seconds_until_next_utc_run()
            log.info("Next nightly analysis in %.0f s (00:05 UTC)", delay)
            await asyncio.sleep(delay)

            from btc_edge.db.session import Database
            from btc_edge.ml.nightly_analyzer import report_text, run_nightly_analysis
            from btc_edge.web.app import _alerter

            db = Database(settings.db_path)
            try:
                base_config = dict(registry.champion.config) if registry.champion else {}
                report = run_nightly_analysis(
                    db.session(),
                    base_config=base_config,
                    market_key="btc_15m",
                    current_min_edge=settings.trading.min_ev_threshold,
                    current_entry_seconds=900.0 - settings.trading.entry_warmup_seconds,
                )
            finally:
                db.close()

            engine_state.nightly_analysis = report
            if _alerter.enabled:
                with contextlib.suppress(Exception):
                    asyncio.create_task(_alerter.send(report_text(report)))
            log.info(
                "Nightly analysis complete: %d windows, auto_apply=%s",
                report.get("window_count", 0),
                report.get("auto_apply"),
            )
        except Exception as exc:  # noqa: BLE001 - the scheduler must never die
            log.warning("Nightly analyzer error: %s", exc)
            await asyncio.sleep(60.0)


async def _ollama_loop(gate, engine_state) -> None:
    """Pre-load the local Ollama 7B execution gate and keep it warm + visible.

    On startup this blocks until the model is resident (long timeout), then every
    ~15s it confirms the model is still loaded (re-warming after an Ollama restart)
    and mirrors the gate's live stats into ``engine_state.ollama_status`` so the
    dashboard's blinking indicator tracks it.
    """
    log = logging.getLogger("ollama_loop")
    if gate is None:
        engine_state.ollama_status["state"] = "disabled"
        engine_state.ollama_status["model"] = ""
        return
    engine_state.ollama_status["model"] = gate.model
    engine_state.ollama_status["state"] = "loading"
    ok = await gate.warmup()
    engine_state.ollama_status["state"] = "ready" if ok else "offline"
    if ok:
        print(f"Ollama 7B gate loaded and warm: {gate.model}")
    else:
        print("Ollama 7B gate FAILED to load (falling back to Hold-safe gate)")
    while True:
        await asyncio.sleep(15.0)
        try:
            loaded = await gate.is_loaded()
            if not loaded:
                engine_state.ollama_status["state"] = "loading"
                await gate.warmup()
                loaded = await gate.is_loaded()
            engine_state.ollama_status["state"] = "ready" if loaded else "offline"
            s = gate.status()
            engine_state.ollama_status.update({
                "model": s.get("model", gate.model),
                "last_verdict": s.get("last_verdict", ""),
                "last_latency_ms": s.get("last_latency_ms", 0.0),
                "num_verdicts": s.get("num_verdicts", 0),
                "num_vetoes": s.get("num_vetoes", 0),
                "num_timeouts": s.get("num_timeouts", 0),
            })
        except Exception as exc:  # noqa: BLE001 - heartbeat must never die
            log.warning("Ollama heartbeat error: %s", exc)


async def _shadow_engine_loop(engine_state, ops) -> None:
    """Nonstop paper (shadow) trading on BOTH venues, graded every 15m round.

    Never reaches an exchange order endpoint — it only writes into the ops
    monitor's trade log with ``mode='shadow'``. Grades each venue's round on that
    venue's own settlement rule (spot vs the round's open strike).
    """
    from btc_edge.execution.ops_monitor import grade_round

    log = logging.getLogger("shadow_engine")
    interval = 15.0
    open_trades: dict[str, dict[str, Any]] = {}
    placed_window: dict[str, int] = {}
    graded = 0
    n = 0
    while True:
        try:
            now = time.time()
            ws = int(now) // 900 * 900
            for venue in ("kalshi", "polymarket"):
                # Rollover: grade the previous round's shadow trade.
                if venue in placed_window and placed_window[venue] != 0 and placed_window[venue] < ws:
                    strike = engine_state.open_strike
                    spot = (engine_state.spot_prices or {}).get(venue)
                    if strike is not None and spot is not None:
                        up = spot >= strike
                        t = open_trades.pop(venue, None)
                        if t is not None:
                            for g in grade_round([t], up):
                                ops.grade_trade(t["id"], g)
                            graded += 1
                    placed_window[venue] = ws
                # Place a fresh shadow trade once per round per venue.
                prob = engine_state.model_prob.get(venue)
                if prob is not None and venue not in open_trades:
                    side = "UP" if prob >= 0.5 else "DOWN"
                    cents = _shadow_ask_cents(engine_state, venue, side)
                    t = {
                        "id": f"shadow-{venue}-{ws}-{n}",
                        "venue": venue, "mode": "shadow", "side": side,
                        "qty": 1, "cents": cents, "bot": "shadow-champion",
                        "ts": now, "status": "open",
                    }
                    ops.record_trade(t)
                    open_trades[venue] = t
                    placed_window[venue] = ws
                    n += 1
                    log.info("shadow %s %s @ %sc", venue, side, cents)
            ops.beat("shadow-engine", {"progress": graded + n})
        except Exception as exc:  # noqa: BLE001 - the shadow loop must never die
            log.warning("shadow engine error: %s", exc)
        await asyncio.sleep(interval)


def _shadow_ask_cents(engine_state, venue: str, side: str) -> int:
    """Approximate the real best-ask for a side (cents), for honest shadow entry."""
    prob_up = 0.5
    if venue == "kalshi":
        kal = engine_state.kalshi_market or {}
        prob_up = float(kal.get("prob_up") or 0.5)
    else:
        pm = (engine_state.polymarket_markets or {}).get("15m") or {}
        prob_up = float(pm.get("prob_up") or pm.get("up") or 0.5)
    mid = prob_up if side == "UP" else (1.0 - prob_up)
    # 1-cent half-spread, clamped to the tradable 1..99 range.
    ask = mid + 0.01
    return max(1, min(99, round(ask * 100)))


def main() -> int:
    settings = load_settings()
    _setup_logging(settings.logging.level)

    # Mirror live state + risk limits into the dashboard.
    engine_state.mode = settings.trading.mode
    killswitch = KillSwitch(settings.safety.kill_switch_file)
    engine_state.kill_switch = killswitch.is_armed()
    engine_state.risk = {
        "at_risk_usd": 0.0,
        "daily_loss_limit_usd": settings.trading.bankroll * settings.trading.max_daily_loss_pct / 100.0,
        "bankroll": settings.trading.bankroll,
    }

    # Initialize the SQLite database (signals, fills, settlements, rule grades).
    db = Database(settings.db_path)
    _ = db  # keep the handle alive for the process lifetime

    # Rehydrate the Champion/Challenger strategy registry from any prior run.
    try:
        from btc_edge.ml.strategy_registry import load_registry

        load_registry(db.session(), _registry)
    except Exception as exc:  # noqa: BLE001 - registry load is best-effort
        print(f"Strategy registry load skipped: {exc}")

    # Build the news-intelligence pipeline.
    scraper = NewsScraper(
        min_relevance=settings.news.min_relevance,
        max_items_per_source=settings.news.max_items_per_source,
    )
    analyzer = SentimentAnalyzer(backend=settings.news.sentiment_backend)
    filt = SelfHealingFilter(
        window=settings.news.window,
        min_signals=settings.news.min_signals,
        downgrade_threshold=settings.news.downgrade_threshold,
        disable_threshold=settings.news.disable_threshold,
        promote_threshold=settings.news.promote_threshold,
    )
    intelligence = NewsIntelligence(scraper, analyzer, filt)

    # Start the news loop as a background task alongside the web server.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(_news_loop(settings, intelligence))

    # Start the live market feed (BTC price, Polymarket + Kalshi markets,
    # window countdowns) so the dashboard shows live, accurate data.
    feed = LiveMarketFeed(
        engine_state,
        gamma_base_url=settings.polymarket.gamma_api_url,
        kalshi_base_url=settings.kalshi.active_base_url,
        kalshi_ticker=settings.kalshi.market_ticker,
    )
    loop.create_task(feed.run())

    # Dual real-time spot feeds (free public WebSockets): Kalshi synthetic BRTI
    # vs Polymarket Chainlink — genuinely distinct per-venue prices + lead/lag.
    from btc_edge.connectors.spot_feeds import DualSpotFeed

    spot_feed = DualSpotFeed(engine_state)
    loop.create_task(spot_feed.run())

    # Build live order-routing clients (only if API keys are present).
    from btc_edge.connectors.kalshi_client import KalshiClient, build_kalshi_signer_from_env
    from btc_edge.connectors.poly_client import PolymarketUSClient

    kalshi_client: Any | None = None
    signer = build_kalshi_signer_from_env()
    if signer is not None:
        kalshi_client = KalshiClient(signer=signer, base_url=settings.kalshi.active_base_url)

    poly_client: Any | None = None
    # Polymarket US (polymarket.us) authenticates with a Key ID + Secret Key from
    # polymarket.us/developer — NOT the EVM wallet used on polymarket.com.
    pm_key_id = os.getenv("POLYMARKET_API_KEY_ID")
    pm_secret = os.getenv("POLYMARKET_API_SECRET")
    if pm_key_id and pm_secret:
        poly_client = PolymarketUSClient(key_id=pm_key_id, secret_key=pm_secret)

    # Start the trading loop: strategies → router → paper/live settlement.
    from btc_edge.db.brain import BrainStore

    brain = BrainStore()
    # Hydrate the dashboard score from the persisted ledger so W/L + net P&L
    # survive a restart and display instantly (before the first API poll).
    for _venue in ("kalshi", "polymarket"):
        _r = brain.get_rollup(_venue)
        if _r:
            engine_state.venue_stats.setdefault(_venue, {}).update({
                "wins": int(_r.get("wins", 0)),
                "losses": int(_r.get("losses", 0)),
                "pnl_usd": float(_r.get("total_pnl_usd", 0.0)),
            })
    trader = TradingLoop(
        engine_state, settings, feed,
        kalshi_client=kalshi_client, poly_client=poly_client,
        registry=_registry, brain=brain,
    )
    loop.create_task(trader.run())

    # Register the live Ollama gate so the dashboard's model picker can switch it.
    set_ollama_gate(trader.ollama_gate)

    # Warm-start the Kalshi model on real settled windows (best-effort, async).
    loop.create_task(trader.seed_from_history())

    # Pre-load the local Ollama 7B execution gate + keep it warm, and mirror its
    # live status to the dashboard's blinking indicator.
    loop.create_task(_ollama_loop(trader.ollama_gate, engine_state))

    # Refresh REAL portfolio data (balances, fills, settlements, positions).
    loop.create_task(_portfolio_loop(settings, engine_state, kalshi_client, poly_client, brain))

    # Nightly late-night analyzer: once daily at 00:05 UTC, calibrate + grid-search.
    loop.create_task(_nightly_analyzer_loop(settings, engine_state, _registry))

    # DeepSeek oracle: an independent LLM voter blended into the ensemble's prob.
    from btc_edge.ml.deepseek_oracle import build_oracle

    deepseek = build_oracle(settings)
    if deepseek.configured:
        loop.create_task(_deepseek_loop(settings, engine_state, deepseek))
        print("DeepSeek oracle enabled (AI_API_KEY present)")
    else:
        print("DeepSeek oracle disabled (no AI_API_KEY)")

    # Kalshi reconciler: separate AI_LIVE / HUMAN_MANUAL / AI_SHADOW attribution and
    # push a scoreboard report to Telegram every ~15 minutes.
    loop.create_task(_reconciler_loop(settings, engine_state, kalshi_client))

    # Command Deck: register the read-only readiness probes and start the nonstop
    # paper (shadow) trading engine, both supervised for liveness.
    from btc_edge.execution.ops_probes import build_probes

    set_ops_probes(build_probes(settings, engine_state, kalshi_client, poly_client))
    loop.create_task(_shadow_engine_loop(engine_state, get_ops_monitor()))

    print("BTC-Edge engine starting (live-only)")
    print(f"Live trading allowed: {settings.live_trading_allowed()}")
    print(f"Trade-only keys enforced: {settings.safety.trade_only_keys}")

    port = int(os.getenv("PORT", settings.web.port))
    print(f"Dashboard: http://{settings.web.host}:{port}")

    import uvicorn

    config = uvicorn.Config(app, host=settings.web.host, port=port, loop="asyncio")
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())
    return 0


def _acquire_single_instance_lock(port: int = 54321):
    """Bind a localhost TCP socket to enforce a single running engine.

    Windows frees the port the instant the process exits or crashes, so this is
    robust against stale PID files and against the venv stub-launcher that spawns
    the base interpreter (a venv-parent / base-child pair is still ONE engine).
    Returns the bound socket, or ``None`` if another engine already holds it.
    """
    import contextlib
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        # SO_EXCLUSIVEADDRUSE prevents a second bind even with SO_REUSEADDR set.
        with contextlib.suppress(OSError):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


if __name__ == "__main__":
    lock = _acquire_single_instance_lock()
    if lock is None:
        print("Another BTC-Edge engine is already running (port 54321 in use). Exiting.")
        sys.exit(1)
    try:
        sys.exit(main())
    finally:
        lock.close()

