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


def test_get_btc_markets_uses_series_ticker(monkeypatch):
    from btc_edge.connectors.kalshi_client import KalshiClient

    client = KalshiClient(signer=None, base_url="http://localhost")
    calls: list[tuple] = []

    async def fake_request(method, path, *, params=None, json_body=None):
        calls.append((method, path, params))
        return {"markets": []}

    monkeypatch.setattr(client, "_request", fake_request)
    asyncio.run(client.get_btc_markets())
    assert calls[0][0] == "GET"
    assert calls[0][1] == "/markets"
    assert calls[0][2]["series_ticker"] == "KXBTC"


def test_resolve_btc_markets_picks_nearest():
    from btc_edge.connectors.kalshi_client import resolve_btc_markets

    markets = [
        {"ticker": "KXBTC-X-B50000", "strike_type": "less", "cap_strike": 50000},
        {"ticker": "KXBTC-X-B70000", "strike_type": "less", "cap_strike": 70000},
        {"ticker": "KXBTC-X-B80000", "strike_type": "less", "cap_strike": 80000},
        {"ticker": "KXBTC-X-T90000", "strike_type": "greater", "floor_strike": 90000},
        {"ticker": "KXBTC-X-T100000", "strike_type": "greater", "floor_strike": 100000},
        {"ticker": "KXBTC-X-T110000", "strike_type": "greater", "floor_strike": 110000},
    ]
    above, below = resolve_btc_markets(markets, current_price=77300)
    assert above is not None and above["ticker"] == "KXBTC-X-T90000"
    assert below is not None and below["ticker"] == "KXBTC-X-B80000"


def test_resolve_btc_markets_skips_expired():
    from btc_edge.connectors.kalshi_client import resolve_btc_markets

    markets = [
        # Already expired (close_time in the past)
        {"ticker": "KXBTC-X-T90000", "strike_type": "greater", "floor_strike": 90000,
         "close_time": "2020-01-01T05:00:00Z"},
        {"ticker": "KXBTC-X-T100000", "strike_type": "greater", "floor_strike": 100000,
         "close_time": "2099-01-01T05:00:00Z"},
    ]
    above, _ = resolve_btc_markets(markets, current_price=77300)
    assert above is not None and above["ticker"] == "KXBTC-X-T100000"
