"""Tests for dashboard authentication (finding A1)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from btc_edge.web.app import app


def test_api_requires_auth_when_key_set(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_KEY", "secret123")
    client = TestClient(app)

    # No key -> 401.
    assert client.get("/api/status").status_code == 401
    # Wrong key -> 401.
    assert client.get("/api/status", headers={"X-API-Key": "wrong"}).status_code == 401
    # Correct key -> 200.
    assert client.get("/api/status", headers={"X-API-Key": "secret123"}).status_code == 200


def test_state_changing_endpoints_require_auth(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_KEY", "secret123")
    client = TestClient(app)

    assert client.post("/api/halt").status_code == 401
    assert client.post("/api/start").status_code == 401
    assert client.post("/api/assistant").status_code == 401
    assert client.post("/api/halt", headers={"X-API-Key": "secret123"}).status_code == 200
    # reset state for other tests
    client.post("/api/start", headers={"X-API-Key": "secret123"})


def test_api_open_in_dev_when_no_key(monkeypatch):
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    client = TestClient(app)
    assert client.get("/api/status").status_code == 200


def test_health_check_is_public(monkeypatch):
    # Health probe must stay reachable (Render) even when auth is on.
    monkeypatch.setenv("DASHBOARD_API_KEY", "secret123")
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
