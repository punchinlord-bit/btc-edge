"""Financial / Bitcoin sentiment analysis.

Converts news text into a normalized sentiment score in ``[-1, +1]`` indicating
positive (bullish) or negative (bearish) market impact.

The default backend is a domain-specific financial lexicon (deterministic,
offline, fully testable). A transformer-based financial model (HuggingFace
``transformers``) is supported as an optional backend and is loaded lazily only
when installed and explicitly selected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class SentimentResult:
    score: float       # -1.0 (bearish) .. +1.0 (bullish)
    confidence: float  # 0.0 .. 1.0
    label: str         # bullish | bearish | neutral

    @property
    def market_impact(self) -> float:
        """Signed impact in [-1, 1] (positive = upward pressure)."""
        return self.score


# --------------------------------------------------------------------------
# Domain-specific financial / Bitcoin sentiment lexicon (weighted).
# --------------------------------------------------------------------------
POSITIVE_TERMS: dict[str, float] = {
    "surge": 1.0, "surges": 1.0, "surged": 1.0, "surging": 1.0,
    "rally": 0.9, "rallying": 0.9, "rallied": 0.9,
    "bullish": 1.0, "bull": 0.7, "bulls": 0.7, "bullrun": 1.0, "bull run": 1.0,
    "breakout": 0.9, "break out": 0.8,
    "soar": 1.0, "soars": 1.0, "soared": 1.0, "soaring": 1.0,
    "spike": 0.8, "spikes": 0.8, "spiked": 0.8,
    "pump": 0.7, "pumped": 0.7, "pumping": 0.7,
    "gain": 0.6, "gains": 0.7, "gained": 0.7,
    "rebound": 0.7, "rebounds": 0.7, "rebounded": 0.7,
    "recover": 0.7, "recovery": 0.7, "recovered": 0.7,
    "record high": 1.0, "all-time high": 1.0, "ath": 0.9,
    "milestone": 0.7, "milestones": 0.7,
    "adopt": 0.6, "adoption": 0.8, "adopted": 0.7,
    "approve": 0.8, "approved": 0.9, "approval": 0.9,
    "institutional": 0.7,
    "inflow": 0.8, "inflows": 0.8,
    "accumulation": 0.6, "accumulate": 0.6,
    "upside": 0.8, "moon": 0.8, "mooning": 0.8,
    "buy": 0.5, "buying": 0.6, "bought": 0.6,
    "outperform": 0.7, "outperformed": 0.7, "outperforms": 0.7,
    "beat": 0.5, "beats": 0.5, "beating": 0.5,
    "growth": 0.7, "grow": 0.5, "growing": 0.6, "grew": 0.6,
    "expand": 0.5, "expansion": 0.6, "expanding": 0.6,
    "partnership": 0.6, "partnerships": 0.6,
    "launch": 0.5, "launches": 0.5, "launched": 0.5,
    "upgrade": 0.5, "upgraded": 0.5, "upgrades": 0.5,
    "positive": 0.6, "optimistic": 0.7, "optimism": 0.7,
    "confidence": 0.6, "confident": 0.6,
    "strong": 0.6, "stronger": 0.7, "strengthen": 0.7, "strengthens": 0.7,
    "profit": 0.5, "profitable": 0.6, "profits": 0.5,
    "win": 0.4, "winning": 0.5, "wins": 0.4,
    "success": 0.5, "successful": 0.6,
    "support": 0.4, "supportive": 0.5, "supported": 0.4,
    "uptrend": 0.8, "higher": 0.6, "rise": 0.6, "rises": 0.6, "rising": 0.7,
    "climb": 0.6, "climbs": 0.6, "climbing": 0.7,
    "increase": 0.5, "increases": 0.5, "increasing": 0.6, "increased": 0.6,
    "halving": 0.7,
}

NEGATIVE_TERMS: dict[str, float] = {
    "crash": 1.0, "crashed": 1.0, "crashes": 1.0,
    "dump": 0.9, "dumped": 0.9, "dumping": 0.9, "dumps": 0.9,
    "bearish": 1.0, "bear": 0.7, "bears": 0.7,
    "selloff": 0.9, "sell-off": 0.9, "sell off": 0.9,
    "plunge": 0.9, "plunges": 0.9, "plunged": 0.9, "plunging": 0.9,
    "plummet": 0.9, "plummeted": 0.9, "plummeting": 0.9,
    "drop": 0.6, "drops": 0.6, "dropped": 0.7,
    "fall": 0.6, "falls": 0.6, "falling": 0.7, "fell": 0.7,
    "decline": 0.7, "declines": 0.7, "declined": 0.7, "declining": 0.7,
    "slump": 0.8, "slumped": 0.8, "slumping": 0.8,
    "slide": 0.7, "slides": 0.7, "slid": 0.7,
    "sink": 0.7, "sinks": 0.7, "sinking": 0.7, "sank": 0.7,
    "tumble": 0.8, "tumbled": 0.8, "tumbling": 0.8,
    "capitulation": 0.9, "capitulate": 0.8,
    "panic": 0.8, "panicked": 0.8,
    "fear": 0.8, "fearful": 0.8,
    "fud": 0.9,
    "hack": 0.9, "hacked": 0.9, "hacks": 0.9,
    "exploit": 0.9, "exploited": 0.9, "exploits": 0.9,
    "ban": 0.9, "banned": 0.9, "banning": 0.9,
    "crackdown": 0.8, "crackdowns": 0.8,
    "regulatory": 0.6, "regulation": 0.6, "regulator": 0.5,
    "lawsuit": 0.7, "sue": 0.7, "sued": 0.7,
    "fraud": 0.9, "fraudulent": 0.9,
    "scam": 0.9, "scams": 0.9,
    "rug pull": 1.0, "rugpull": 1.0,
    "liquidate": 0.7, "liquidation": 0.8, "liquidations": 0.8, "liquidated": 0.8,
    "outflow": 0.8, "outflows": 0.8,
    "sell": 0.5, "selling": 0.6, "sold": 0.6,
    "short": 0.5, "shorting": 0.6,
    "underperform": 0.6, "underperformed": 0.6,
    "miss": 0.4, "misses": 0.4, "missed": 0.5,
    "loss": 0.6, "losses": 0.7,
    "negative": 0.6, "pessimistic": 0.7, "pessimism": 0.7,
    "weak": 0.6, "weaker": 0.7, "weaken": 0.7, "weakens": 0.7,
    "reject": 0.6, "rejected": 0.7, "rejection": 0.7,
    "delay": 0.5, "delayed": 0.5,
    "uncertainty": 0.6, "uncertain": 0.6,
    "risk": 0.5, "risky": 0.6, "risks": 0.5,
    "warning": 0.6, "warn": 0.6, "warns": 0.6, "warned": 0.6,
    "concern": 0.5, "concerns": 0.6, "concerned": 0.6,
    "threat": 0.6, "threats": 0.6, "threatening": 0.6,
    "downtrend": 0.8, "lower": 0.6,
    "bear market": 0.9, "correction": 0.6,
    "bubble": 0.7, "burst": 0.8, "bursts": 0.8,
    "depeg": 0.9, "depegged": 0.9,
}

NEGATIONS: set[str] = {
    "not", "no", "never", "neither", "nor", "without", "don't", "dont",
    "doesn't", "doesnt", "isn't", "isnt", "wasn't", "wasnt", "won't", "wont",
    "can't", "cant", "cannot", "couldn't", "couldnt",
}

INTENSIFIERS: set[str] = {
    "very", "extremely", "hugely", "massively", "significantly", "sharply",
    "strongly", "deeply", "severely", "greatly", "particularly", "especially",
    "dramatically", "rapidly",
}

_WORD_RE = re.compile(r"[a-zA-Z]+(?:['-][a-zA-Z]+)*")


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _bigramify(tokens: list[str]) -> list[str]:
    """Yield unigrams and adjacent bigrams for multi-word lexicon terms."""
    grams: list[str] = list(tokens)
    grams += [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]
    return grams


class LexiconSentimentModel:
    """Deterministic financial-lexicon sentiment scorer."""

    def analyze(self, text: str) -> SentimentResult:
        if not text:
            return SentimentResult(score=0.0, confidence=0.0, label="neutral")

        tokens = _tokenize(text)
        grams = _bigramify(tokens)

        pos = 0.0
        neg = 0.0
        matched = 0
        negation_pending = False

        for tok in tokens:
            if tok in NEGATIONS:
                negation_pending = True
                continue

            intensity = 1.5 if tok in INTENSIFIERS else 1.0
            score = 0.0
            if tok in POSITIVE_TERMS:
                score = POSITIVE_TERMS[tok]
            elif tok in NEGATIVE_TERMS:
                score = -NEGATIVE_TERMS[tok]

            if score != 0.0:
                matched += 1
                if negation_pending:
                    score = -score
                if score > 0:
                    pos += score * intensity
                else:
                    neg += -score * intensity
            negation_pending = False

        # Multi-word terms (checked against the bigrams).
        for g in grams:
            if g in POSITIVE_TERMS and g not in tokens:
                pos += POSITIVE_TERMS[g]
                matched += 1
            elif g in NEGATIVE_TERMS and g not in tokens:
                neg += NEGATIVE_TERMS[g]
                matched += 1

        total = pos + neg
        if total <= 0:
            return SentimentResult(score=0.0, confidence=0.0, label="neutral")

        score = (pos - neg) / total
        score = max(-1.0, min(1.0, score))
        confidence = min(1.0, matched / 3.0)
        label = "bullish" if score > 0.15 else ("bearish" if score < -0.15 else "neutral")
        return SentimentResult(score=score, confidence=confidence, label=label)


class TransformerSentimentModel:
    """Optional HuggingFace transformer backend for financial sentiment.

    Loaded lazily only when ``transformers`` (and a model) are available; raises
    a clear error otherwise so the caller can fall back to the lexicon model.
    """

    def __init__(
        self,
        model_name: str = "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis",
    ) -> None:
        self.model_name = model_name
        self._pipe: Any = None

    def _ensure(self) -> None:
        if self._pipe is None:
            try:
                from transformers import pipeline  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "transformers is not installed; use the lexicon backend instead"
                ) from exc
            self._pipe = pipeline("sentiment-analysis", model=self.model_name)

    def analyze(self, text: str) -> SentimentResult:
        self._ensure()
        result = self._pipe(text[:512])[0]
        label = str(result["label"]).lower()
        raw = float(result["score"])
        if label in {"positive", "bullish"}:
            score = raw
        elif label in {"negative", "bearish"}:
            score = -raw
        else:
            score = 0.0
        out_label = "bullish" if score > 0.15 else ("bearish" if score < -0.15 else "neutral")
        return SentimentResult(score=score, confidence=raw, label=out_label)


class SentimentAnalyzer:
    """Unified sentiment interface with a pluggable backend."""

    def __init__(self, backend: str = "lexicon", model_name: str | None = None) -> None:
        if backend == "transformer":
            name = model_name or "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis"
            self.model: Any = TransformerSentimentModel(name)
        else:
            self.model = LexiconSentimentModel()

    def analyze(self, text: str) -> SentimentResult:
        return self.model.analyze(text)

    def analyze_batch(self, texts: list[str]) -> list[SentimentResult]:
        return [self.model.analyze(t) for t in texts]

    def aggregate(self, results: list[SentimentResult]) -> float:
        """Combine multiple sentiment results into one score in [-1, 1].

        Confidence-weighted average; neutral (zero-confidence) results are
        ignored so they do not dilute the signal.
        """
        if not results:
            return 0.0
        weighted = 0.0
        total_conf = 0.0
        for r in results:
            if r.confidence <= 0:
                continue
            weighted += r.score * r.confidence
            total_conf += r.confidence
        if total_conf <= 0:
            return 0.0
        return max(-1.0, min(1.0, weighted / total_conf))
