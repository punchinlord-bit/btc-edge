"""Tests for the strategy registry (in-memory + SQLite persistence)."""

from __future__ import annotations

from btc_edge.ml.promotion_gate import ARCHIVED, CHAMPION, WindowEvaluation
from btc_edge.ml.strategy_registry import Registry, load_registry, save_registry


def _win(ts: int, regime: str, outcome: int, x1: float) -> WindowEvaluation:
    return WindowEvaluation(
        window_start_sec=ts,
        strike_price=90000.0,
        close_price=90000.0 + (1.0 if outcome else -1.0),
        outcome=outcome,
        regime=regime,
        features={"x1": x1},
    )


def test_registry_evaluates_and_promotes():
    from btc_edge.ml.promotion_gate import PromotionGate

    reg = Registry(gate=PromotionGate(min_oos_windows=20))
    reg.register_champion("v1", {"bias": 0.0, "x1": 0.0})

    ts = 1_700_000_000
    for regime in ("chop", "trend", "spike"):
        for i in range(20):
            x1 = 1.0 if i % 2 == 0 else -1.0
            reg.record_window(_win(ts, regime, 1 if x1 > 0 else 0, x1))
            ts += 900

    result = reg.evaluate_challenger("v2", {"bias": 0.0, "x1": 3.0})
    assert result["passed"] is True
    assert reg.champion_id == "v2"
    assert reg.versions["v1"].status == ARCHIVED
    assert reg.versions["v2"].status == CHAMPION
    assert reg.telemetry()["oos_window_count"] == 60


def test_registry_sqlite_roundtrip():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from btc_edge.db.models import Base

    reg = Registry()
    reg.register_champion("v1", {"bias": 0.0, "x1": 0.0})
    reg.record_window(_win(1_700_000_000, "trend", 1, 1.0))

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    save_registry(session, reg)
    session.close()

    session2 = Session()
    loaded = load_registry(session2)
    assert loaded.champion_id == "v1"
    assert loaded.versions["v1"].config == {"bias": 0.0, "x1": 0.0}
    assert len(loaded.windows) == 1
    assert loaded.windows[0].regime == "trend"
