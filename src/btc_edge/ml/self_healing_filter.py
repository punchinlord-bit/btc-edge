"""Self-healing news-source filter — the "brain" of the intelligence pipeline.

Cross-references past news signals with actual price settlements to compute a
per-source predictive accuracy. Sources that consistently produce noise or false
signals are autonomously downgraded (reduced weight) or disabled (zero weight),
and are only promoted back once their trailing accuracy recovers. The output is
a reliability-weighted aggregate sentiment that feeds the ensemble estimator.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from btc_edge.connectors.news_scraper import NewsItem
from btc_edge.features.sentiment_analyzer import SentimentAnalyzer, SentimentResult

ACTIVE = "ACTIVE"
DOWNGRADED = "DOWNGRADED"
DISABLED = "DISABLED"

DIRECTION_THRESHOLD = 0.15  # |sentiment| below this is treated as "no call"


@dataclass(slots=True)
class SourceStats:
    source: str
    rolling: deque[bool] = field(default_factory=deque)
    signals: int = 0
    status: str = ACTIVE

    @property
    def accuracy(self) -> float:
        if not self.rolling:
            return 0.5
        return sum(self.rolling) / len(self.rolling)

    @property
    def n_evaluated(self) -> int:
        return len(self.rolling)


@dataclass(slots=True)
class PendingSignal:
    source: str
    sentiment: float
    confidence: float
    timestamp: float


@dataclass(slots=True)
class NewsSignal:
    """A reliability-weighted sentiment signal ready for the ensemble."""

    source: str
    sentiment: float
    confidence: float
    weight: float
    timestamp: float


class SelfHealingFilter:
    """Tracks source accuracy and weights aggregate sentiment by reliability."""

    def __init__(
        self,
        window: int = 200,
        min_signals: int = 5,
        downgrade_threshold: float = 0.45,
        disable_threshold: float = 0.35,
        promote_threshold: float = 0.55,
    ) -> None:
        self.window = window
        self.min_signals = min_signals
        self.downgrade_threshold = downgrade_threshold
        self.disable_threshold = disable_threshold
        self.promote_threshold = promote_threshold
        self._sources: dict[str, SourceStats] = {}
        self._pending: list[PendingSignal] = []

    def register(self, source: str) -> None:
        if source not in self._sources:
            self._sources[source] = SourceStats(
                source=source, rolling=deque(maxlen=self.window)
            )

    def register_many(self, sources: list[str]) -> None:
        for s in sources:
            self.register(s)

    def record_signal(
        self,
        source: str,
        sentiment: float,
        confidence: float = 1.0,
        timestamp: float | None = None,
    ) -> None:
        """Record a pending news signal awaiting a settlement outcome."""
        self.register(source)
        self._pending.append(PendingSignal(
            source=source,
            sentiment=sentiment,
            confidence=confidence,
            timestamp=timestamp if timestamp is not None else time.time(),
        ))

    @staticmethod
    def _predicted_direction(sentiment: float) -> int:
        if sentiment > DIRECTION_THRESHOLD:
            return 1
        if sentiment < -DIRECTION_THRESHOLD:
            return -1
        return 0

    def observe(self, source: str, sentiment: float, direction: int) -> bool:
        """Directly evaluate one (sentiment -> direction) pair and return whether
        the call was correct."""
        self.register(source)
        stats = self._sources[source]
        predicted = self._predicted_direction(sentiment)
        correct = predicted != 0 and predicted == direction
        stats.rolling.append(correct)
        stats.signals += 1
        self._update_status(stats)
        return correct

    def settle(self, direction: int) -> list[str]:
        """Evaluate all pending signals against a realized price direction.

        Returns the list of sources whose status changed as a result.
        """
        changed: list[str] = []
        if not self._pending:
            return changed
        for p in self._pending:
            old = self._sources.get(p.source)
            before = old.status if old is not None else ACTIVE
            self.observe(p.source, p.sentiment, direction)
            after = self._sources[p.source].status
            if before != after:
                changed.append(p.source)
        self._pending.clear()
        return changed

    def _update_status(self, stats: SourceStats) -> None:
        if stats.n_evaluated < self.min_signals:
            return
        acc = stats.accuracy
        if stats.status == DISABLED:
            # Self-heal: only promote back once accuracy clearly recovers.
            if acc >= self.promote_threshold:
                stats.status = ACTIVE
            return
        if acc < self.disable_threshold:
            stats.status = DISABLED
        elif acc < self.downgrade_threshold:
            stats.status = DOWNGRADED
        else:
            stats.status = ACTIVE

    # -- weighting ----------------------------------------------------------

    def source_weight(self, source: str) -> float:
        stats = self._sources.get(source)
        if stats is None:
            return 0.0
        if stats.n_evaluated < self.min_signals:
            return 0.5
        if stats.status == DISABLED:
            return 0.0
        acc = stats.accuracy
        if stats.status == DOWNGRADED:
            return acc * 0.5
        return acc

    def aggregate(self, signals: list[tuple[str, float, float]]) -> float:
        """Reliability-weighted aggregate sentiment in [-1, 1]."""
        if not signals:
            return 0.0
        weighted = 0.0
        total_w = 0.0
        for source, sentiment, confidence in signals:
            w = self.source_weight(source) * confidence
            if w <= 0:
                continue
            weighted += sentiment * w
            total_w += w
        if total_w <= 0:
            return 0.0
        return max(-1.0, min(1.0, weighted / total_w))

    # -- introspection ------------------------------------------------------

    def statuses(self) -> dict[str, str]:
        return {s: st.status for s, st in self._sources.items()}

    def accuracies(self) -> dict[str, float]:
        return {s: st.accuracy for s, st in self._sources.items()}

    def active_sources(self) -> list[str]:
        return [s for s, st in self._sources.items() if st.status != DISABLED]

    def disabled_sources(self) -> list[str]:
        return [s for s, st in self._sources.items() if st.status == DISABLED]

    def pending_count(self) -> int:
        return len(self._pending)


class NewsIntelligence:
    """Orchestrates the scraper, sentiment analyzer, and self-healing filter.

    ``process`` fetches news, scores sentiment, records signals against each
    source, and returns the reliability-weighted aggregate sentiment plus the
    per-item ``NewsSignal`` objects ready to feed the ensemble estimator.
    """

    def __init__(
        self,
        scraper,
        analyzer: SentimentAnalyzer,
        filter_: SelfHealingFilter,
    ) -> None:
        self.scraper = scraper
        self.analyzer = analyzer
        self.filter = filter_

    def analyze_items(self, items: list[NewsItem]) -> list[NewsSignal]:
        """Score each item and attach its current source reliability weight."""
        signals: list[NewsSignal] = []
        for item in items:
            result: SentimentResult = self.analyzer.analyze(item.text)
            weight = self.filter.source_weight(item.source)
            signals.append(NewsSignal(
                source=item.source,
                sentiment=result.score,
                confidence=result.confidence,
                weight=weight,
                timestamp=item.published_ts,
            ))
        return signals

    def aggregate_from_items(self, items: list[NewsItem]) -> float:
        signals = self.analyze_items(items)
        return self.filter.aggregate(
            [(s.source, s.sentiment, s.confidence) for s in signals]
        )

    async def process(self) -> tuple[float, list[NewsSignal]]:
        """Fetch -> analyze -> aggregate; returns (aggregate_sentiment, signals)."""
        items = await self.scraper.fetch()
        signals = self.analyze_items(items)
        aggregate = self.filter.aggregate(
            [(s.source, s.sentiment, s.confidence) for s in signals]
        )
        return aggregate, signals

    def record_and_settle(self, items: list[NewsItem], direction: int) -> list[str]:
        """Record each item's sentiment as a pending signal, then settle all of
        them against a realized price direction."""
        for item in items:
            result = self.analyzer.analyze(item.text)
            self.filter.record_signal(
                item.source, result.score, result.confidence, item.published_ts
            )
        return self.filter.settle(direction)

