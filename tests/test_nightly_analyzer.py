"""Tests for the nightly late-night analyzer (calibration + grid search)."""

from __future__ import annotations

import json
import time

from btc_edge.ml.nightly_analyzer import (
    calibration_report,
    grid_search,
    report_text,
    run_nightly_analysis,
)
from btc_edge.ml.promotion_gate import WindowEvaluation


def _win(ts: int, outcome: int, features: dict[str, float], regime: str = "trend") -> WindowEvaluation:
    return WindowEvaluation(
        window_start_sec=ts,
        strike_price=90000.0,
        close_price=90000.0 + (1.0 if outcome else -1.0),
        outcome=outcome,
        regime=regime,
        market_key="btc_15m",
        features=features,
        market_ask_up=0.52,
        market_ask_down=0.52,
        fee_rate=0.015,
    )


def test_calibration_report_buckets_by_confidence_and_regime():
    # A strong positive weight on x1 -> high confidence when x1 > 0.
    windows = [
        _win(1_700_000_000 + i * 900, 1 if i % 2 == 0 else 0, {"x1": 2.0 if i % 2 == 0 else -2.0})
        for i in range(20)
    ]
    rep = calibration_report(windows, config={"x1": 3.0})
    assert rep["n"] == 20
    assert 0.0 <= rep["brier"] <= 1.0
    assert rep["win_rate"] == 0.5  # alternating outcomes
    # Every prediction lands in a high-confidence bucket (sigmoid(6) ~ 0.998).
    assert rep["confidence_buckets"], "expected at least one populated bucket"
    assert rep["confidence_buckets"][0]["hi"] >= 0.95
    assert rep["regime_buckets"]["trend"]["n"] == 20


def test_grid_search_finds_positive_ev_config():
    # x1 perfectly predicts the outcome; a positive weight should dominate 0.5.
    windows = [
        _win(1_700_000_000 + i * 900, 1 if i % 2 == 0 else 0, {"x1": 2.0 if i % 2 == 0 else -2.0})
        for i in range(40)
    ]
    result = grid_search(
        windows,
        base_config={"x1": 3.0},
        min_edge_grid=(0.0, 0.01),
        entry_seconds_grid=(900.0,),
    )
    best = result["best"]
    assert best, "grid search must return a best result"
    assert best["total_pnl"] > 0.0
    assert best["bets"] > 0


def test_run_nightly_analysis_writes_candidate(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from btc_edge.db.models import Base, Btc15mWindowEvaluation

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    now = int(time.time())
    for i in range(4):
        up = i % 2 == 0
        session.add(
            Btc15mWindowEvaluation(
                market_key="btc_15m",
                window_start_sec=now - 900 * (4 - i),
                strike_price=90000.0,
                close_price=90000.0 + (1.0 if up else -1.0),
                outcome=1 if up else 0,
                regime="trend",
                features_json=json.dumps(
                    {"x1": 2.0 if up else -2.0, "time_remaining_norm": 0.0}
                ),
                market_ask_up=0.52,
                market_ask_down=0.52,
                fee_rate=0.015,
            )
        )
    session.commit()

    out = tmp_path / "candidate_config.json"
    report = run_nightly_analysis(
        session,
        base_config={"x1": 3.0},
        candidate_path=out,
    )
    assert report["window_count"] == 4
    assert report["candidate_path"] == str(out)
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "actual" in data and "optimal" in data
    assert "auto_apply" in data
    assert "recommended" in data
    assert "min_ev_threshold" in data["recommended"]["trading"]


def test_report_text_clean_line():
    report = {
        "window_count": 96,
        "actual": {"total_pnl": -1.2, "bets": 30},
        "optimal": {"total_pnl": 4.5, "bets": 30},
        "auto_apply": True,
    }
    text = report_text(report)
    # Clean, plain-English single line — no Brier / calibration buckets.
    assert "Nightly tune-up" in text
    assert "-1.20" in text
    assert "Brier" not in text
