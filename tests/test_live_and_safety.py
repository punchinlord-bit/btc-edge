"""Tests for the live-deployment hardening: kill switch, audit log, idempotency,
forward-test evaluation, AI assistant fallback, and web endpoints."""

from __future__ import annotations

import pytest

from btc_edge.execution.audit import AuditLog
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.execution.router import idempotency_key
from btc_edge.historical.forward_test import (
    Prediction,
    compute_metrics,
    report_text,
)
from btc_edge.ml.ai_assistant import AIAssistant

# ------------------------------------------------------------- kill switch

def test_killswitch_persists(tmp_path):
    ks = KillSwitch(tmp_path / "ks.json")
    assert not ks.is_armed()
    ks.arm()
    assert ks.is_armed()
    # A fresh instance reading the same file sees the armed state (out-of-band).
    ks2 = KillSwitch(tmp_path / "ks.json")
    assert ks2.is_armed()
    ks2.reset()
    assert not ks2.is_armed()


# ----------------------------------------------------------------- audit log

def test_audit_log_append_only(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record("kill_switch", armed=True, source="test")
    audit.record("order", venue="kalshi", side="up")
    entries = audit.read()
    assert len(entries) == 2
    assert entries[0]["event"] == "kill_switch"
    assert entries[0]["armed"] is True
    assert entries[1]["venue"] == "kalshi"


# ------------------------------------------------------------- idempotency

def test_idempotency_key_deterministic():
    a = idempotency_key("kalshi", "up", "buy", 0.5, 10.0, ts=1234)
    b = idempotency_key("kalshi", "up", "buy", 0.5, 10.0, ts=1234)
    assert a == b
    # Different params -> different key.
    c = idempotency_key("kalshi", "up", "buy", 0.6, 10.0, ts=1234)
    assert a != c


# ------------------------------------------------------------ forward test

def test_forward_test_metrics():
    preds = [
        Prediction(ts=1, prob=0.6, outcome=1.0),  # win, pnl +0.4
        Prediction(ts=2, prob=0.6, outcome=0.0),  # loss, pnl -0.6
        Prediction(ts=3, prob=0.7, outcome=1.0),  # win, pnl +0.3
    ]
    r = compute_metrics(preds)
    assert r.n == 3
    assert r.net_ev == pytest.approx((0.4 - 0.6 + 0.3) / 3)
    assert r.win_rate == pytest.approx(2 / 3)
    assert r.brier_score == pytest.approx(((0.4) ** 2 + (0.6) ** 2 + (0.3) ** 2) / 3)
    assert "POSITIVE" in report_text(r)  # net EV is positive -> POSITIVE EDGE


def test_forward_test_empty():
    r = compute_metrics([])
    assert r.n == 0
    assert r.net_ev == 0.0


# ------------------------------------------------------------ AI assistant

def test_assistant_offline_fallback():
    a = AIAssistant(api_key="")  # no key -> offline
    assert not a.configured
    snapshot = {"mode": "paper", "kill_switch": False, "regime": "DAMPENING",
                "news_sentiment": 0.3, "risk": {"at_risk_usd": 5.0, "daily_loss_limit_usd": 500.0},
                "signals": ["bullish"]}
    text = a.offline_explain(snapshot)
    assert "paper" in text.upper() or "PAPER" in text
    assert "bullish" in text


def test_assistant_offline_explain_async():
    import asyncio

    a = AIAssistant(api_key="")
    reply = asyncio.run(a.explain({"mode": "paper", "kill_switch": False,
                                   "regime": "DAMPENING", "news_sentiment": 0.0,
                                   "risk": {}, "signals": []}))
    assert reply.backend == "offline"
    assert reply.text


# ------------------------------------------------------------- web endpoints

def test_web_health_and_overview(monkeypatch):
    # This test exercises the web endpoints themselves, not auth — so make the
    # auth state deterministic by clearing any configured dashboard key.
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    import btc_edge.web.app as app_module

    monkeypatch.setattr(app_module._settings, "dashboard_api_key", "")

    from fastapi.testclient import TestClient

    from btc_edge.web.app import app

    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    o = client.get("/api/overview")
    assert o.status_code == 200
    assert "advice" in o.json()

    # Halt then resume.
    h = client.post("/api/halt")
    assert h.json()["kill_switch"] is True
    s = client.post("/api/start")
    assert s.json()["kill_switch"] is False
