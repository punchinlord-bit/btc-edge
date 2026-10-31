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
import sys

from btc_edge.config import load_settings
from btc_edge.connectors.news_scraper import NewsScraper
from btc_edge.db.session import Database
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.features.sentiment_analyzer import SentimentAnalyzer
from btc_edge.ml.self_healing_filter import NewsIntelligence, SelfHealingFilter
from btc_edge.web.app import app, engine_state


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
            # Derive simple human-readable signals from the aggregate.
            if aggregate > 0.25:
                engine_state.signals = ["News is bullish — bot leans toward adding."]
            elif aggregate < -0.25:
                engine_state.signals = ["News is bearish — bot leans toward caution."]
            else:
                engine_state.signals = ["News is neutral — waiting for a setup."]
            log.info("News loop: %d items, sentiment %+.3f", len(items), aggregate)
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            log.warning("News loop error: %s", exc)
        await asyncio.sleep(refresh)


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

    print(f"BTC-Edge engine starting in mode={settings.trading.mode!r}")
    print(f"Live trading allowed: {settings.live_trading_allowed()}")
    print(f"Trade-only keys enforced: {settings.safety.trade_only_keys}")

    import os

    port = int(os.getenv("PORT", settings.web.port))
    print(f"Dashboard: http://{settings.web.host}:{port}")

    import uvicorn

    config = uvicorn.Config(app, host=settings.web.host, port=port, loop="asyncio")
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())

