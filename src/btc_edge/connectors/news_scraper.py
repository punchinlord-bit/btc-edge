"""Asynchronous news scraper for Bitcoin market-moving events.

Fetches headlines and summaries from a curated list of top crypto / financial
RSS feeds, parses RSS 2.0 and Atom, and filters strictly for Bitcoin-related
items using a weighted keyword relevance score.

The network layer is fully async (``httpx``) and isolated from the pure parsing
/ relevance logic so the scraper is trivially unit-testable offline.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NewsItem:
    source: str
    title: str
    summary: str
    url: str
    published_ts: float
    relevance: float = 0.0

    @property
    def item_id(self) -> str:
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:16]

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}"


# Curated RSS feeds for Bitcoin market intelligence.
CURATED_SOURCES: list[dict[str, str]] = [
    {"name": "coindesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "cointelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "bitcoinmagazine", "url": "https://bitcoinmagazine.com/feed"},
    {"name": "decrypt", "url": "https://decrypt.co/feed"},
    {"name": "bitcoinist", "url": "https://bitcoinist.com/feed/"},
    {"name": "newsbtc", "url": "https://www.newsbtc.com/feed/"},
]

# Weighted keywords for Bitcoin relevance scoring (weight = signal strength).
BTC_KEYWORDS: dict[str, float] = {
    "bitcoin": 1.0,
    "btc": 1.0,
    "satoshi": 0.8,
    "bitcoin etf": 1.0,
    "spot etf": 0.9,
    "halving": 0.9,
    "hash rate": 0.7,
    "hashrate": 0.7,
    "crypto": 0.5,
    "blockchain": 0.5,
    "miner": 0.6,
    "mining": 0.6,
    "on-chain": 0.7,
    "onchain": 0.7,
    "lightning network": 0.7,
    "ordinals": 0.6,
}


def _parse_feed(xml_text: str) -> list[dict]:
    """Parse an RSS 2.0 or Atom feed into a list of item dicts."""
    root = ET.fromstring(xml_text.strip())
    tag = _local_tag(root.tag)
    if tag == "feed":
        return _parse_atom(root)
    return _parse_rss(root)


def _local_tag(tag: str) -> str:
    """Strip the XML namespace prefix from a tag."""
    return tag.split("}", 1)[-1]


def _parse_rss(root: ET.Element) -> list[dict]:
    items: list[dict] = []
    for item in root.iter():
        if _local_tag(item.tag) != "item":
            continue
        title = _find_text(item, "title")
        summary = _find_text(item, "description") or _find_text(item, "summary")
        link = _find_text(item, "link")
        date = _find_text(item, "pubDate") or _find_text(item, "date")
        items.append({
            "title": title,
            "summary": _strip_html(summary),
            "url": link,
            "published_ts": _parse_date(date),
        })
    return items


def _parse_atom(root: ET.Element) -> list[dict]:
    items: list[dict] = []
    for entry in root.iter():
        if _local_tag(entry.tag) != "entry":
            continue
        title = _find_text(entry, "title")
        summary = _find_text(entry, "summary") or _find_text(entry, "content")
        link = _find_link(entry)
        date = _find_text(entry, "updated") or _find_text(entry, "published")
        items.append({
            "title": title,
            "summary": _strip_html(summary),
            "url": link,
            "published_ts": _parse_date(date),
        })
    return items


def _find_text(parent: ET.Element, tag: str) -> str:
    for child in parent:
        if _local_tag(child.tag) == tag and child.text:
            return child.text.strip()
    return ""


def _find_link(parent: ET.Element) -> str:
    for child in parent:
        if _local_tag(child.tag) == "link":
            href = child.get("href")
            if href:
                return href
            if child.text:
                return child.text.strip()
    return ""


def _strip_html(text: str) -> str:
    """Best-effort removal of HTML/XML markup and CDATA wrappers."""
    if not text:
        return ""
    out = text
    if out.startswith("<![CDATA["):
        out = out[9:-3]
    out = re.sub(r"<[^>]+>", " ", out)
    out = re.sub(r"&[a-zA-Z#0-9]+;", " ", out)
    return " ".join(out.split())


def _parse_date(date_str: str) -> float:
    if not date_str:
        return 0.0
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.timestamp()
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime

        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def relevance_score(title: str, summary: str, keywords: dict[str, float]) -> float:
    """Weighted Bitcoin relevance score in [0, 1].

    Title matches are weighted 3x higher than summary matches (headlines carry
    the signal).
    """
    title_l = title.lower()
    summary_l = summary.lower()
    score = 0.0
    for kw, w in keywords.items():
        if kw in title_l:
            score += w * 3.0
        elif kw in summary_l:
            score += w
    # A single top keyword (weight 1.0) in the title maps to relevance 1.0.
    return min(1.0, score / 3.0)


class NewsScraper:
    """Async scraper over curated RSS feeds, filtering Bitcoin-relevant items."""

    def __init__(
        self,
        sources: list[dict[str, str]] | None = None,
        keywords: dict[str, float] | None = None,
        min_relevance: float = 0.15,
        max_items_per_source: int = 20,
        timeout: float = 15.0,
    ) -> None:
        self.sources = sources if sources is not None else list(CURATED_SOURCES)
        self.keywords = keywords if keywords is not None else dict(BTC_KEYWORDS)
        self.min_relevance = min_relevance
        self.max_items_per_source = max_items_per_source
        self.timeout = timeout

    def parse_feed(self, source_name: str, xml_text: str) -> list[NewsItem]:
        """Parse one feed's XML and produce BTC-relevant ``NewsItem`` objects."""
        raw = _parse_feed(xml_text)
        items: list[NewsItem] = []
        for r in raw[: self.max_items_per_source]:
            title = r["title"]
            summary = r["summary"]
            if not title:
                continue
            rel = relevance_score(title, summary, self.keywords)
            if rel < self.min_relevance:
                continue
            items.append(NewsItem(
                source=source_name,
                title=title,
                summary=summary,
                url=r["url"] or "",
                published_ts=r["published_ts"] or time.time(),
                relevance=rel,
            ))
        return items

    async def fetch_source(self, name: str, url: str) -> list[NewsItem]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "btc-edge/0.1"})
                resp.raise_for_status()
                return self.parse_feed(name, resp.text)
        except Exception as exc:  # noqa: BLE001 - one dead source must not block the rest
            logger.warning("News source %s failed: %s", name, exc)
            return []

    async def fetch(self) -> list[NewsItem]:
        """Fetch and filter all curated sources concurrently."""
        import asyncio

        tasks = [self.fetch_source(s["name"], s["url"]) for s in self.sources]
        results = await asyncio.gather(*tasks)
        items: list[NewsItem] = []
        for batch in results:
            items.extend(batch)
        # De-duplicate by URL and sort newest-first.
        seen: set[str] = set()
        deduped: list[NewsItem] = []
        for item in sorted(items, key=lambda i: i.published_ts, reverse=True):
            if item.item_id in seen:
                continue
            seen.add(item.item_id)
            deduped.append(item)
        return deduped

    def fetch_sync(self) -> list[NewsItem]:
        """Synchronous convenience wrapper (for scripts and tests)."""
        import asyncio

        return asyncio.run(self.fetch())
