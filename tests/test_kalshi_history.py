"""Tests for Kalshi trade-history methods (fills / settlements)."""

from __future__ import annotations

import asyncio

from btc_edge.connectors.kalshi_client import KalshiClient


def test_get_fills_path_and_params(monkeypatch):
    client = KalshiClient(signer=None, base_url="http://localhost")
    calls: list[tuple] = []

    async def fake_request(method, path, *, params=None, json_body=None):
        calls.append((method, path, params))
        return {"fills": [], "cursor": None}

    monkeypatch.setattr(client, "_request", fake_request)
    asyncio.run(client.get_fills(ticker="KXBTC15M", limit=100, min_ts=1234))
    assert calls[0][0] == "GET"
    assert calls[0][1] == "/portfolio/fills"
    assert calls[0][2]["ticker"] == "KXBTC15M"
    assert calls[0][2]["limit"] == 100
    assert calls[0][2]["min_ts"] == 1234


def test_get_settlements_path_and_params(monkeypatch):
    client = KalshiClient(signer=None, base_url="http://localhost")
    calls: list[tuple] = []

    async def fake_request(method, path, *, params=None, json_body=None):
        calls.append((method, path, params))
        return {"settlements": []}

    monkeypatch.setattr(client, "_request", fake_request)
    asyncio.run(client.get_settlements(ticker="KXBTC15M", limit=50))
    assert calls[0][0] == "GET"
    assert calls[0][1] == "/portfolio/settlements"
    assert calls[0][2]["ticker"] == "KXBTC15M"
    assert calls[0][2]["limit"] == 50
