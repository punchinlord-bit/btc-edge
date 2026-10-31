"""Strategy base classes and shared market-context types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MarketContext:
    """A complete feature snapshot at an instant in a market window."""

    venue: str
    timeframe_seconds: int
    window_ts: int
    window_open_price: float
    current_spot: float
    time_remaining: float
    contract_price: float  # market mid of the "up" contract
    prob_estimate: float  # ensemble probability (0..1)
    window_delta_pct: float
    option_implied_prob: float
    book_imbalance: float
    micro_acceleration: float
    regime: str
    news_sentiment: float = 0.0
    bids: list[tuple[float, float]] = field(default_factory=list)  # (price, size)
    asks: list[tuple[float, float]] = field(default_factory=list)
    realized_vol: float = 0.0
    implied_vol: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 1.0

    def features(self) -> dict[str, float]:
        """Feature dict consumed by the pattern engine."""
        dampening = 1.0 if self.regime == "DAMPENING" else 0.0
        amplifying = 1.0 if self.regime == "AMPLIFYING" else 0.0
        return {
            "time_remaining": self.time_remaining,
            "contract_price": self.contract_price,
            "prob_estimate": self.prob_estimate,
            "window_delta_pct": self.window_delta_pct,
            "option_implied_prob": self.option_implied_prob,
            "book_imbalance": self.book_imbalance,
            "micro_acceleration": self.micro_acceleration,
            "news_sentiment": self.news_sentiment,
            "realized_vol": self.realized_vol,
            "implied_vol": self.implied_vol,
            "current_spot": self.current_spot,
            "window_open_price": self.window_open_price,
            "regime_dampening": dampening,
            "regime_amplifying": amplifying,
        }


@dataclass(slots=True)
class StrategyDecision:
    strategy: str
    side: str  # up | down
    action: str  # buy | sell
    price: float
    size: float
    order_type: str  # maker | taker
    reason: str
    prob_estimate: float
    ev_estimate: float
    rule_snapshot: dict[str, Any] = field(default_factory=dict)


class Strategy(ABC):
    name: str = "base"

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> list[StrategyDecision]:
        """Return a list of decisions for the given context (possibly empty)."""
