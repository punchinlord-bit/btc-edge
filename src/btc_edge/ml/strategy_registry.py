"""Champion/Challenger strategy registry backed by SQLite.

Wraps :class:`~btc_edge.ml.promotion_gate.PromotionGate` with a small in-memory
registry that keeps the active ``CHAMPION`` version, accumulates out-of-sample
15-minute windows, and evaluates proposed ``CHALLENGER``s. Results are persisted
to the ``strategy_versions`` / ``btc_15m_window_evaluations`` tables.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from btc_edge.ml.promotion_gate import (
    ARCHIVED,
    CHAMPION,
    PromotionGate,
    StrategyVersion,
    WindowEvaluation,
)


@dataclass(slots=True)
class Registry:
    gate: PromotionGate = field(default_factory=PromotionGate)
    versions: dict[str, StrategyVersion] = field(default_factory=dict)
    champion_id: str | None = None
    windows: list[WindowEvaluation] = field(default_factory=list)
    last_result: dict[str, Any] = field(default_factory=dict)

    @property
    def champion(self) -> StrategyVersion | None:
        return self.versions.get(self.champion_id) if self.champion_id else None

    def register_champion(self, version_id: str, config: dict[str, float]) -> StrategyVersion:
        v = StrategyVersion(version_id=version_id, status=CHAMPION, config=config)
        self.versions[version_id] = v
        self.champion_id = version_id
        return v

    def record_window(self, window: WindowEvaluation) -> None:
        self.windows.append(window)

    def evaluate_challenger(self, version_id: str, config: dict[str, float]) -> dict[str, Any]:
        challenger = StrategyVersion(version_id=version_id, config=config)
        result = self.gate.evaluate_and_promote(challenger, self.windows, self.champion)
        self.versions[version_id] = challenger
        if result.passed:
            if self.champion_id and self.champion_id != version_id:
                old = self.versions.get(self.champion_id)
                if old is not None:
                    old.status = ARCHIVED
            self.champion_id = version_id
        self.last_result = {
            "passed": result.passed,
            "challenger_version_id": result.challenger_version_id,
            "champion_version_id": result.champion_version_id,
            "challenger_brier": result.challenger_brier,
            "champion_brier": result.champion_brier,
            "challenger_net_ev": result.challenger_net_ev,
            "champion_net_ev": result.champion_net_ev,
            "regime_win_rates": result.regime_win_rates,
            "jitter_retention": result.jitter_retention,
            "checks": result.checks,
            "reasons": result.reasons,
        }
        return self.last_result

    def telemetry(self) -> dict[str, Any]:
        champ = self.champion
        return {
            "champion_version_id": self.champion_id,
            "champion_status": champ.status if champ else None,
            "champion_config": champ.config if champ else {},
            "oos_window_count": len(self.windows),
            "last_result": self.last_result,
        }


def save_registry(session: Any, registry: Registry) -> None:
    """Persist the registry's versions and windows to SQLite."""
    from btc_edge.db.models import Btc15mWindowEvaluation
    from btc_edge.db.models import StrategyVersion as StrategyVersionModel

    for v in registry.versions.values():
        row = session.get(StrategyVersionModel, v.version_id)
        if row is None:
            row = StrategyVersionModel(version_id=v.version_id)
            session.add(row)
        row.market_key = v.market_key
        row.parent_version_id = v.parent_version_id
        row.status = v.status
        row.config_json = json.dumps(v.config)
        row.metrics_json = json.dumps(v.metrics)
        row.gate_checks_json = json.dumps(v.gate_checks)
        row.created_by = v.created_by
        row.evaluated_at = time.time()

    for w in registry.windows:
        row = session.get(Btc15mWindowEvaluation, w.window_start_sec)
        if row is None:
            row = Btc15mWindowEvaluation(
                window_start_sec=w.window_start_sec,
                strike_price=w.strike_price,
                close_price=w.close_price,
                outcome=w.outcome,
                regime=w.regime,
            )
            session.add(row)
        row.features_json = json.dumps(w.features)
        row.market_ask_up = w.market_ask_up
        row.market_ask_down = w.market_ask_down
        row.fee_rate = w.fee_rate
    session.commit()


def load_registry(session: Any, registry: Registry | None = None) -> Registry:
    """Rehydrate a registry from SQLite (best-effort)."""
    from btc_edge.db.models import Btc15mWindowEvaluation
    from btc_edge.db.models import StrategyVersion as StrategyVersionModel

    registry = registry or Registry()
    for row in session.query(StrategyVersionModel).all():
        v = StrategyVersion(
            version_id=row.version_id,
            market_key=row.market_key,
            parent_version_id=row.parent_version_id,
            status=row.status,
            config=json.loads(row.config_json or "{}"),
            metrics=json.loads(row.metrics_json or "{}"),
            gate_checks=json.loads(row.gate_checks_json or "{}"),
            created_by=row.created_by,
        )
        registry.versions[v.version_id] = v
        if v.status == CHAMPION:
            registry.champion_id = v.version_id

    for row in session.query(Btc15mWindowEvaluation).all():
        registry.windows.append(WindowEvaluation(
            window_start_sec=row.window_start_sec,
            strike_price=row.strike_price,
            close_price=row.close_price,
            outcome=row.outcome,
            regime=row.regime,
            features=json.loads(row.features_json or "{}"),
            market_ask_up=row.market_ask_up,
            market_ask_down=row.market_ask_down,
            fee_rate=row.fee_rate,
        ))
    return registry
