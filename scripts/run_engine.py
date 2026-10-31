"""BTC-Edge live engine entry point.

Loads configuration, initializes the database, wires connectors (when API keys
are present), starts the safety watchdog + the news-intelligence loop, and serves
the FastAPI dashboard. Runs in paper mode by default; live routing remains
blocked unless the dual ``trading.mode == "live"`` AND ``ALLOW_LIVE_TRADING ==
"TRUE"`` gate passes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
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
from btc_edge.web.app import _registry, app, engine_state
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


async def _portfolio_loop(settings, engine_state, kalshi_client, poly_client, pm_wallet) -> None:
    """Periodically refresh REAL portfolio data (balances, fills, settlements,
    positions) from Kalshi + Polymarket and publish it to the dashboard."""
    from btc_edge.execution.portfolio_poller import poll_kalshi, poll_polymarket

    log = logging.getLogger("portfolio_loop")
    ticker = settings.kalshi.market_ticker
    interval = max(15, int(getattr(settings, "portfolio_poll_seconds", 30) or 30))
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
                recent.extend(k.recent_trades)
                open_pos.extend(k.open_positions)
                for key in totals:
                    totals[key] += k.trade_totals.get(key, 0.0)
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.warning("Kalshi portfolio poll error: %s", exc)
        if poly_client is not None:
            try:
                p = await poll_polymarket(poly_client, wallet=pm_wallet)
                engine_state.wallet_balances["polymarket"] = p.balance
                engine_state.venue_stats["polymarket"] = p.venue_stats
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
        await asyncio.sleep(interval)


def main() -> int:
    settings = load_settings()
    _setup_logging(settings.logging.level)

    # Mirror paper-first state + risk limits into the dashboard.
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

    # Build live order-routing clients (only if API keys are present).
    from btc_edge.connectors.kalshi_client import KalshiClient, build_kalshi_signer_from_env
    from btc_edge.connectors.poly_client import GammaClient, PolymarketClient

    kalshi_client: Any | None = None
    signer = build_kalshi_signer_from_env()
    if signer is not None:
        kalshi_client = KalshiClient(signer=signer, base_url=settings.kalshi.active_base_url)

    poly_client: Any | None = None
    pm_wallet = os.getenv("POLYMARKET_WALLET") or settings.polymarket_wallet
    pm_key = os.getenv("POLYMARKET_PRIVATE_KEY") or settings.polymarket_private_key
    if pm_wallet and pm_key:
        poly_client = PolymarketClient(
            gamma=GammaClient(settings.polymarket.gamma_api_url),
            wallet=pm_wallet,
            private_key=pm_key,
        )

    # Start the trading loop: strategies → router → paper/live settlement.
    trader = TradingLoop(
        engine_state, settings, feed,
        kalshi_client=kalshi_client, poly_client=poly_client,
        registry=_registry,
    )
    loop.create_task(trader.run())

    # Refresh REAL portfolio data (balances, fills, settlements, positions).
    loop.create_task(_portfolio_loop(settings, engine_state, kalshi_client, poly_client, pm_wallet))

    print(f"BTC-Edge engine starting in mode={settings.trading.mode!r}")
    print(f"Live trading allowed: {settings.live_trading_allowed()}")
    print(f"Trade-only keys enforced: {settings.safety.trade_only_keys}")

    port = int(os.getenv("PORT", settings.web.port))
    print(f"Dashboard: http://{settings.web.host}:{port}")

    import uvicorn

    config = uvicorn.Config(app, host=settings.web.host, port=port, loop="asyncio")
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())

