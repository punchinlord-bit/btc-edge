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
    assert calls[0][2]["series_ticker"] == "KXBTC15M"


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


def test_resolve_btc_15m_market_picks_active_window():
    from btc_edge.connectors.kalshi_client import resolve_btc_15m_market

    markets = [
        {"ticker": "KXBTC15M-PAST", "open_time": "2026-01-01T00:00:00Z",
         "close_time": "2026-01-01T00:15:00Z"},
        {"ticker": "KXBTC15M-ACTIVE", "open_time": "2026-01-01T00:15:00Z",
         "close_time": "2026-01-01T00:30:00Z"},
        {"ticker": "KXBTC15M-FUTURE", "open_time": "2026-01-01T00:30:00Z",
         "close_time": "2026-01-01T00:45:00Z"},
    ]
    now = 1767226800.0  # 2026-01-01T00:20:00Z (inside ACTIVE window)
    m = resolve_btc_15m_market(markets, now_ts=now)
    assert m is not None and m["ticker"] == "KXBTC15M-ACTIVE"


def test_resolve_btc_15m_market_falls_back_to_future():
    from btc_edge.connectors.kalshi_client import resolve_btc_15m_market

    markets = [
        {"ticker": "KXBTC15M-PAST", "open_time": "2026-01-01T00:00:00Z",
         "close_time": "2026-01-01T00:15:00Z"},
        {"ticker": "KXBTC15M-FUTURE", "open_time": "2026-01-01T00:30:00Z",
         "close_time": "2026-01-01T00:45:00Z"},
    ]
    now = 1767226800.0  # 2026-01-01T00:20:00Z (PAST closed at 00:15, only FUTURE remains)
    m = resolve_btc_15m_market(markets, now_ts=now)
    assert m is not None and m["ticker"] == "KXBTC15M-FUTURE"


def test_intra_account_transfer_payload(monkeypatch):
    """The transfer moves USD (shard 0) -> crypto (shard 2) with centicents."""
    client = KalshiClient(signer=None, base_url="http://localhost")
    calls: list[tuple] = []

    async def fake_request(method, path, *, params=None, json_body=None):
        calls.append((method, path, json_body))
        return {"transfer_id": "t-1"}

    monkeypatch.setattr(client, "_request", fake_request)
    asyncio.run(client.intra_account_transfer(amount_centicents=50_000))
    method, path, body = calls[0]
    assert method == "POST"
    assert path == "/portfolio/intra_exchange_instance_transfer"
    assert body["source"] == "event_contract"
    assert body["destination"] == "event_contract"
    assert body["amount"] == 50_000  # $5.00 in centicents
    assert body["source_exchange_shard"] == 0  # USD
    assert body["destination_exchange_shard"] == 2  # crypto/BTC
    assert body["client_transfer_id"]


def test_ensure_crypto_balance_converts_when_low(monkeypatch):
    client = KalshiClient(signer=None, base_url="http://localhost")
    requests: list[tuple] = []

    async def fake_request(method, path, *, params=None, json_body=None):
        requests.append((method, path, json_body))
        if path == "/portfolio/balance":
            return {"balance_breakdown": [
                {"balance": "8.70", "exchange_index": 0},
                {"balance": "0.01", "exchange_index": 2},
            ]}
        return {"transfer_id": "t-1"}

    monkeypatch.setattr(client, "_request", fake_request)
    converted = asyncio.run(client.ensure_crypto_balance(min_dollars=5.0))
    assert converted is True
    # First call is the balance read; second is the transfer.
    assert requests[1][0] == "POST"
    assert requests[1][1] == "/portfolio/intra_exchange_instance_transfer"
    # $5.00 - $0.01 = $4.99 -> 49_900 centicents.
    assert requests[1][2]["amount"] == 49_900


def test_ensure_crypto_balance_noop_when_sufficient(monkeypatch):
    client = KalshiClient(signer=None, base_url="http://localhost")
    requests: list[tuple] = []

    async def fake_request(method, path, *, params=None, json_body=None):
        requests.append((method, path, json_body))
        return {"balance_breakdown": [
            {"balance": "3.00", "exchange_index": 0},
            {"balance": "6.00", "exchange_index": 2},
        ]}

    monkeypatch.setattr(client, "_request", fake_request)
    converted = asyncio.run(client.ensure_crypto_balance(min_dollars=5.0))
    assert converted is False
    assert len(requests) == 1  # only the balance read, no transfer
