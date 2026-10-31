"""CLI entry point for the news intelligence pipeline.

Fetches Bitcoin headlines, scores sentiment, and reports the reliability-weighted
aggregate plus per-source health. Falls back gracefully when offline.

Usage:
    python scripts/run_news.py
"""

from __future__ import annotations

import asyncio
import sys

from btc_edge.config import load_settings
from btc_edge.connectors.news_scraper import NewsScraper
from btc_edge.features.sentiment_analyzer import SentimentAnalyzer
from btc_edge.ml.self_healing_filter import NewsIntelligence, SelfHealingFilter


def main() -> int:
    settings = load_settings()
    cfg = settings.news

    scraper = NewsScraper(
        min_relevance=cfg.min_relevance,
        max_items_per_source=cfg.max_items_per_source,
    )
    analyzer = SentimentAnalyzer(backend=cfg.sentiment_backend)
    filter_ = SelfHealingFilter(
        window=cfg.window,
        min_signals=cfg.min_signals,
        downgrade_threshold=cfg.downgrade_threshold,
        disable_threshold=cfg.disable_threshold,
        promote_threshold=cfg.promote_threshold,
    )
    intelligence = NewsIntelligence(scraper, analyzer, filter_)

    aggregate, signals = asyncio.run(intelligence.process())

    print(f"Fetched {len(signals)} Bitcoin-relevant headlines")
    print(f"Aggregate sentiment: {aggregate:+.3f}")
    for s in sorted(signals, key=lambda x: abs(x.sentiment), reverse=True)[:10]:
        print(f"  [{s.source:14s}] w={s.weight:.2f} c={s.confidence:.2f} "
              f"s={s.sentiment:+.2f}")
    print("Source statuses:", filter_.statuses())
    return 0


if __name__ == "__main__":
    sys.exit(main())
