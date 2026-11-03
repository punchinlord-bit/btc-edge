"""Tests for keyword-gated "gusto" recovery mode (aggressive Polymarket betting)."""

from __future__ import annotations

import pytest

from btc_edge.config import load_settings
from btc_edge.execution.router import Router, full_kelly_size, quarter_kelly_size
from btc_edge.strategies.base import StrategyDecision


def test_full_kelly_size_no_discount():
    # Full Kelly uses the whole (prob-price)/(1-price) fraction, not the 0.35x
    # fractional-Kelly discount used by quarter_kelly_size.
    fk = full_kelly_size(prob=0.6, price=0.5, bankroll=100.0, cap_pct=100.0)
    assert fk == pytest.approx(20.0, rel=1e-6)  # (0.6-0.5)/(1-0.5) * 100 = 20
    qk = quarter_kelly_size(prob=0.6, price=0.5, bankroll=100.0, cap_pct=100.0)
    assert fk > qk  # full Kelly is strictly larger than fractional


def test_size_for_gusto_lifts_cap_and_discount():
    s = load_settings()
    router = Router(s)
    d = StrategyDecision(
        strategy="market_maker", side="up", action="buy", price=0.50, size=5.0,
        order_type="taker", reason="test", prob_estimate=0.60, ev_estimate=0.05,
    )
    normal = router.size_for(d, venue="polymarket", bankroll=100.0)
    gusto = router.size_for(d, venue="polymarket", bankroll=100.0, gusto=True)
    # Normal mode is clamped to the 5-share minimum by the tight 1% per-trade cap;
    # gusto lifts both the cap and the fractional discount -> a much larger bet.
    assert normal == 5.0
    assert gusto > normal


def test_gusto_endpoint_requires_keyword(monkeypatch):
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    monkeypatch.setenv("GUSTO_MODE_KEYWORD", "swordfish")
    import btc_edge.web.app as app_module

    monkeypatch.setattr(app_module._settings, "dashboard_api_key", "")
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)

    # Wrong keyword -> 403 and stays off.
    r = client.post("/api/gusto-mode", json={"enabled": True, "keyword": "nope"})
    assert r.status_code == 403
    assert app_module.engine_state.gusto_mode is False

    # Correct keyword -> arms.
    r = client.post("/api/gusto-mode", json={"enabled": True, "keyword": "swordfish"})
    assert r.status_code == 200
    assert r.json()["gusto_mode"] is True
    assert app_module.engine_state.gusto_mode is True

    # Disarm is always allowed (no keyword needed).
    r = client.post("/api/gusto-mode", json={"enabled": False})
    assert r.status_code == 200
    assert app_module.engine_state.gusto_mode is False


def test_gusto_endpoint_disabled_without_keyword(monkeypatch):
    # No GUSTO_MODE_KEYWORD configured -> arming is impossible (fails safe).
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    monkeypatch.delenv("GUSTO_MODE_KEYWORD", raising=False)
    import btc_edge.web.app as app_module

    monkeypatch.setattr(app_module._settings, "dashboard_api_key", "")
    monkeypatch.setattr(app_module._settings, "gusto_mode_keyword", "")
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    r = client.post("/api/gusto-mode", json={"enabled": True, "keyword": "anything"})
    assert r.status_code == 403
    assert app_module.engine_state.gusto_mode is False
