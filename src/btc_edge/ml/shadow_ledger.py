"""Shadow (paper-trading) ledger + Brier/Hedge model-weight updater.

Decouples *signal generation* from *execution routing*. When a venue has a $0 (or
below-minimum) balance, the router flips it into SHADOW mode: the exact orderbook
snapshot is recorded, the hypothetical entry is logged, the 15-minute settlement is
scored with the Brier score, sub-agent weights are updated via the exponential
(Hedge) algorithm, and telemetry is dispatched — all without touching the exchange.

Persists to a JSON file (atomic write) so weights survive restarts.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class TradeRecord:
    id: str
    market_id: str
    venue: str
    timestamp: str
    mode: str  # LIVE | SHADOW | SHADOW_FILTERED
    approved: bool
    reason: str
    model_prob_yes: float
    agent_signals: dict[str, float]
    side: str
    entry_price: float
    contracts: int
    cost_dollars: float
    net_edge: float
    status: str = "OPEN"


class ShadowLedger:
    """Atomic JSON persistence for model weights, trade ledger and venue metrics."""

    def __init__(self, storage_path: str = "data/brain_state.json") -> None:
        self.storage_path = storage_path
        self.state = self._load()

    def _defaults(self) -> dict[str, Any]:
        return {
            "modelWeights": {
                "momentumAgent": 0.35,
                "orderflowAgent": 0.35,
                "volatilityAgent": 0.30,
            },
            "ledger": [],
            "metrics": {
                "polymarket": {"livePnL": 0.0, "shadowPnL": 0.0, "wins": 0, "losses": 0,
                               "brierSum": 0.0, "count": 0},
                "kalshi": {"livePnL": 0.0, "shadowPnL": 0.0, "wins": 0, "losses": 0,
                           "brierSum": 0.0, "count": 0},
            },
        }

    def _load(self) -> dict[str, Any]:
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                return self._defaults()
        return self._defaults()

    def save(self) -> None:
        tmp = f"{self.storage_path}.tmp"
        os.makedirs(os.path.dirname(self.storage_path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2)
        os.replace(tmp, self.storage_path)

    def record_trade(self, trade: TradeRecord) -> TradeRecord:
        self.state["ledger"].append(asdict(trade))
        self.save()
        return trade

    def get_unsettled(self, market_id: str | None = None) -> list[dict[str, Any]]:
        open_trades = [t for t in self.state["ledger"] if t.get("status") == "OPEN"]
        if market_id is not None:
            open_trades = [t for t in open_trades if t.get("market_id") == market_id]
        return open_trades



class Brain:
    """Grades LIVE + SHADOW predictions, updates sub-agent weights via Brier/Hedge."""

    def __init__(self, ledger: ShadowLedger, send_telegram=None) -> None:
        self.ledger = ledger
        self.send_telegram = send_telegram or (lambda msg: None)

    def predict(self, agent_signals: dict[str, float]) -> float:
        weights = self.ledger.state["modelWeights"]
        weighted_sum = 0.0
        total_weight = 0.0
        for agent, prob_yes in agent_signals.items():
            w = weights.get(agent, 0.1)
            weighted_sum += prob_yes * w
            total_weight += w
        if total_weight <= 0:
            return 0.5
        return round(weighted_sum / total_weight, 4)

    def record_decision(self, market_id: str, venue: str, agent_signals: dict[str, float],
                        decision, model_prob_yes: float) -> dict[str, Any]:
        trade = TradeRecord(
            id=f"{venue}-{market_id}-{int(time.time() * 1000)}",
            market_id=market_id,
            venue=venue,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            mode=decision.mode,
            approved=decision.approved,
            reason=decision.reason,
            model_prob_yes=model_prob_yes,
            agent_signals=agent_signals,
            side=decision.side,
            entry_price=decision.entry_price,
            contracts=decision.contracts or 0,
            cost_dollars=decision.cost_dollars or 0.0,
            net_edge=decision.net_edge or 0.0,
        )
        self.ledger.record_trade(trade)
        return asdict(trade)

    def settle(self, market_id: str, outcome_yes: int) -> list[dict[str, Any]]:
        """Resolve a 15m market; score Brier and update weights via Hedge."""
        results: list[dict[str, Any]] = []
        for trade in self.ledger.get_unsettled(market_id):
            won = (trade["side"] == "up" and outcome_yes == 1) or (
                trade["side"] == "down" and outcome_yes == 0
            )
            payout = (trade["contracts"] * 1.0) if (trade["approved"] and won) else 0.0
            pnl = round(payout - trade["cost_dollars"], 2) if trade["approved"] else 0.0
            brier = round((trade["model_prob_yes"] - outcome_yes) ** 2, 4)

            trade["status"] = "SETTLED"
            trade["outcomeYes"] = outcome_yes
            trade["won"] = won
            trade["pnl"] = pnl
            trade["brierScore"] = brier

            m = self.ledger.state["metrics"].setdefault(
                trade["venue"],
                {"livePnL": 0.0, "shadowPnL": 0.0, "wins": 0, "losses": 0,
                 "brierSum": 0.0, "count": 0},
            )
            m["count"] += 1
            m["brierSum"] = round(m["brierSum"] + brier, 4)
            if trade["approved"]:
                if won:
                    m["wins"] += 1
                else:
                    m["losses"] += 1
                if trade["mode"] == "LIVE":
                    m["livePnL"] = round(m["livePnL"] + pnl, 2)
                elif trade["mode"] == "SHADOW":
                    m["shadowPnL"] = round(m["shadowPnL"] + pnl, 2)

            # Hedge (exponential multiplicative weights) update with learning rate
            # 0.20 and a hard per-agent clamp [0.05, 0.55] so an out-of-favor
            # mean-reversion agent is never permanently silenced.
            lr = 0.20
            weights = self.ledger.state["modelWeights"]
            for agent, agent_prob in trade["agent_signals"].items():
                loss = (agent_prob - outcome_yes) ** 2
                weights[agent] = weights.get(agent, 0.1) * (2.718281828459045 ** (-lr * loss))
            total = sum(weights.values()) or 1.0
            for agent in weights:
                weights[agent] = round(min(0.55, max(0.05, weights[agent] / total)), 4)

            results.append(trade)

        self.ledger.save()
        return results
