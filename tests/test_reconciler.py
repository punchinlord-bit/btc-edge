"""Tests for the Kalshi reconciler (AI_LIVE / HUMAN_MANUAL / AI_SHADOW attribution)."""

from __future__ import annotations

from btc_edge.execution.reconciler import (
    ai_live_from_ledger,
    build_scoreboard,
    classify_fill,
    format_telegram,
)
from btc_edge.execution.risk_gate import RiskController
from btc_edge.ml.shadow_ledger import Brain, ShadowLedger


def test_classify_fill_ai_prefix():
    assert classify_fill("AI_v3_abc123") == "AI_LIVE"
    assert classify_fill("") == "HUMAN_MANUAL"
    assert classify_fill(None) == "HUMAN_MANUAL"
    assert classify_fill("manual-xyz") == "HUMAN_MANUAL"
    assert classify_fill("AI_v3_") == "AI_LIVE"


def test_attribution_isolates_human_from_ai():
    fills = [
        {"client_order_id": "AI_v3_abc", "yes_price": 50, "count": 5},  # $2.50 notional
        {"client_order_id": "", "yes_price": 50, "count": 5},            # $2.50 notional
    ]
    # $10 payout, $5 total cost -> $5 profit, split 50/50 by notional.
    settlements = [{"revenue": 1000, "yes_total_cost_dollars": "5.0", "no_total_cost_dollars": "0.0"}]
    sb = build_scoreboard(fills, settlements)

    assert sb.ai_live.pnl == 2.5
    assert sb.ai_live.wins == 1
    assert sb.ai_live.losses == 0
    assert sb.human_manual.pnl == 2.5
    assert sb.human_manual.wins == 1
    # Human wins must NOT leak into the AI bucket.
    assert sb.ai_live.wins == 1  # not 2


def test_attribution_human_only_when_no_ai_fills():
    fills = [{"client_order_id": "", "yes_price": 50, "count": 10}]
    settlements = [{"revenue": 0, "yes_total_cost_dollars": "5.0", "no_total_cost_dollars": "0.0"}]
    sb = build_scoreboard(fills, settlements)
    assert sb.ai_live.pnl == 0.0
    assert sb.ai_live.trades == 0
    assert sb.human_manual.pnl == -5.0
    assert sb.human_manual.losses == 1


def test_shadow_metrics_pulled_in():
    fills = []
    settlements = []
    shadow = {"wins": 7, "losses": 3, "shadowPnL": 12.5, "brierSum": 1.8, "count": 10}
    sb = build_scoreboard(fills, settlements, shadow)
    assert sb.ai_shadow.wins == 7
    assert sb.ai_shadow.losses == 3
    assert sb.ai_shadow.pnl == 12.5
    assert sb.ai_shadow.avg_brier == 0.18


def test_format_telegram_clean_line():
    fills = [{"client_order_id": "AI_v3_x", "yes_price": 50, "count": 1}]
    settlements = [{"revenue": 100, "yes_total_cost_dollars": "0.5", "no_total_cost_dollars": "0.0"}]
    sb = build_scoreboard(fills, settlements)
    msg = format_telegram(sb)
    # Clean, plain-English single line — no "Brier" or technical buckets.
    assert "AI so far" in msg
    assert "Brier" not in msg


def test_hedge_weight_clamp(tmp_path):
    ledger = ShadowLedger(str(tmp_path / "brain_state.json"))
    brain = Brain(ledger)
    risk = RiskController()
    # Force the momentum agent to lose repeatedly so its weight would collapse,
    # then verify it's clamped at the 0.05 floor.
    for _ in range(50):
        decision = risk.evaluate_decision(
            venue="kalshi", side="up", win_prob=0.60, entry_price=0.50, live_balance=100.0
        )
        brain.record_decision(
            f"mkt-{_}", "kalshi",
            {"momentumAgent": 0.90, "orderflowAgent": 0.10, "volatilityAgent": 0.10},
            decision, model_prob_yes=0.60,
        )
        brain.settle(f"mkt-{_}", 0)  # momentum predicted 0.90 but outcome was 0
    w = ledger.state["modelWeights"]["momentumAgent"]
    assert 0.05 <= w <= 0.55


def test_ai_live_from_ledger(tmp_path):
    import json

    p = tmp_path / "trades.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "settle", "venue": "kalshi", "pnl": 3.0}) + "\n")
        fh.write(json.dumps({"type": "settle", "venue": "kalshi", "pnl": -1.5}) + "\n")
        fh.write(json.dumps({"type": "settle", "venue": "kalshi", "pnl": 0.0}) + "\n")
        fh.write(json.dumps({"type": "settle", "venue": "polymarket", "pnl": 9.9}) + "\n")
        fh.write(json.dumps({"type": "signal", "venue": "kalshi", "pnl": 99.0}) + "\n")

    m = ai_live_from_ledger(ledger_path=str(p), venue="kalshi")
    assert m["wins"] == 1
    assert m["losses"] == 1
    assert m["pnl"] == 1.5  # 3.0 - 1.5, ignoring 0.0 / polymarket / signal
