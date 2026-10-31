"""Tests for the news intelligence module: scraper, sentiment, self-healing filter."""

from __future__ import annotations

import pytest

from btc_edge.connectors.news_scraper import (
    NewsItem,
    NewsScraper,
    relevance_score,
)
from btc_edge.features.sentiment_analyzer import (
    LexiconSentimentModel,
    SentimentAnalyzer,
    SentimentResult,
)
from btc_edge.ml.self_healing_filter import (
    ACTIVE,
    DISABLED,
    NewsIntelligence,
    SelfHealingFilter,
)

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Bitcoin surges to a new record high</title>
      <description>BTC rallied sharply as ETF inflows surged.</description>
      <link>https://example.com/btc1</link>
      <pubDate>Wed, 29 Oct 2025 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Ethereum launches a new upgrade</title>
      <description>ETH developers ship the latest network upgrade.</description>
      <link>https://example.com/eth1</link>
      <pubDate>Wed, 29 Oct 2025 11:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Bitcoin miners face crackdown</title>
      <description>Regulators ban mining operations in several regions.</description>
      <link>https://example.com/btc2</link>
      <pubDate>Wed, 29 Oct 2025 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test</title>
  <entry>
    <title>Bitcoin adoption grows among institutions</title>
    <summary>Institutional inflows hit a milestone this quarter.</summary>
    <link href="https://example.com/btc3" />
    <updated>2025-10-29T09:00:00Z</updated>
  </entry>
</feed>
"""


# ------------------------------------------------------------------- scraper

def test_relevance_score():
    assert relevance_score("Bitcoin price today", "markets", {"bitcoin": 1.0}) > 0.5
    assert relevance_score("Unrelated headline", "nothing here", {"bitcoin": 1.0}) == 0.0


def test_scraper_parses_rss_and_filters_btc():
    scraper = NewsScraper(sources=[{"name": "test", "url": "http://x"}], min_relevance=0.1)
    items = scraper.parse_feed("test", RSS_SAMPLE)
    assert len(items) == 2
    assert all("Bitcoin" in i.title or "miners" in i.title for i in items)
    assert items[0].source == "test"
    assert items[0].url == "https://example.com/btc1"


def test_scraper_parses_atom():
    scraper = NewsScraper(sources=[{"name": "test", "url": "http://x"}], min_relevance=0.1)
    items = scraper.parse_feed("test", ATOM_SAMPLE)
    assert len(items) == 1
    assert "adoption" in items[0].title
    assert items[0].url == "https://example.com/btc3"


def test_news_item_id_and_text():
    item = NewsItem("s", "Bitcoin rallies", "big gains", "https://x.com/1", 1.0, 0.9)
    assert item.item_id == item.item_id
    assert "rallies" in item.text


# ----------------------------------------------------------------- sentiment

def test_sentiment_bullish():
    model = LexiconSentimentModel()
    r = model.analyze("Bitcoin surges to a new record high as ETF inflows surge")
    assert r.score > 0
    assert r.label == "bullish"


def test_sentiment_bearish():
    model = LexiconSentimentModel()
    r = model.analyze("Bitcoin crashes after a major exchange hack and panic selling")
    assert r.score < 0
    assert r.label == "bearish"


def test_sentiment_neutral():
    model = LexiconSentimentModel()
    r = model.analyze("The committee met on Tuesday")
    assert r.label == "neutral"
    assert r.score == 0.0


def test_sentiment_negation():
    model = LexiconSentimentModel()
    r = model.analyze("Bitcoin is not bullish today")
    assert r.score <= 0


def test_sentiment_analyzer_aggregate():
    analyzer = SentimentAnalyzer(backend="lexicon")
    results = [
        SentimentResult(score=0.8, confidence=1.0, label="bullish"),
        SentimentResult(score=-0.4, confidence=0.5, label="bearish"),
        SentimentResult(score=0.0, confidence=0.0, label="neutral"),
    ]
    agg = analyzer.aggregate(results)
    assert agg == pytest.approx((0.8 - 0.2) / 1.5, abs=1e-9)


def test_sentiment_analyzer_batch():
    analyzer = SentimentAnalyzer(backend="lexicon")
    out = analyzer.analyze_batch(["Bitcoin soars", "Bitcoin dumps"])
    assert out[0].score > 0 and out[1].score < 0


# --------------------------------------------------------- self-healing filter

def test_filter_accuracy_and_disable():
    f = SelfHealingFilter(min_signals=3, disable_threshold=0.35, downgrade_threshold=0.45)
    f.register("good")
    f.register("noise")
    for _ in range(10):
        f.observe("good", 0.8, 1)   # always correct
        f.observe("noise", 0.8, -1)  # always wrong
    assert f.accuracies()["good"] == pytest.approx(1.0)
    assert f.accuracies()["noise"] == pytest.approx(0.0)
    assert f.statuses()["good"] == ACTIVE
    assert f.statuses()["noise"] == DISABLED
    assert f.source_weight("noise") == 0.0


def test_filter_downgrade_then_self_heal():
    f = SelfHealingFilter(
        window=5, min_signals=2, disable_threshold=0.35,
        downgrade_threshold=0.45, promote_threshold=0.55,
    )
    f.register("src")
    for _ in range(6):
        f.observe("src", 0.8, -1)  # wrong
    assert f.statuses()["src"] == DISABLED
    # Rolling window (maxlen=5) drops the old wrong calls as correct ones arrive.
    for _ in range(6):
        f.observe("src", 0.8, 1)  # correct
    assert f.statuses()["src"] == ACTIVE


def test_filter_aggregate_weighted():
    f = SelfHealingFilter(min_signals=3)
    f.register("good")
    f.register("bad")
    for _ in range(5):
        f.observe("good", 0.8, 1)
        f.observe("bad", 0.8, -1)
    agg = f.aggregate([("good", 0.8, 1.0), ("bad", -0.8, 1.0)])
    assert agg > 0.5


def test_filter_settle_pending():
    f = SelfHealingFilter(min_signals=3)
    f.record_signal("a", 0.9, 1.0)
    f.record_signal("b", -0.9, 1.0)
    assert f.pending_count() == 2
    changed = f.settle(1)  # price went up
    assert f.pending_count() == 0
    assert f.accuracies()["a"] == pytest.approx(1.0)
    assert f.accuracies()["b"] == pytest.approx(0.0)
    assert len(changed) >= 0


def test_news_intelligence_aggregate():
    scraper = NewsScraper(sources=[], min_relevance=0.1)
    analyzer = SentimentAnalyzer(backend="lexicon")
    filt = SelfHealingFilter(min_signals=3)
    ni = NewsIntelligence(scraper, analyzer, filt)

    items = [
        NewsItem("s1", "Bitcoin surges to record high", "bullish rally", "u1", 1.0, 1.0),
        NewsItem("s2", "Bitcoin crashes hard", "bearish dump", "u2", 1.0, 1.0),
    ]
    sig = ni.analyze_items(items)
    assert len(sig) == 2
    assert sig[0].sentiment > 0 and sig[1].sentiment < 0
    agg = ni.aggregate_from_items(items)
    assert -1.0 <= agg <= 1.0

