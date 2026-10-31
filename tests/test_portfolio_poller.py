"""Tests for the real portfolio poller (fills / settlements / positions normalization)."""

from __future__ import annotations

import asyncio

from btc_edge.execution.portfolio_poller import (
    _dollars,
    _normalize_price,
    _to_ts,
    poll_kalshi,
    poll_polymarket,
)


def test_to_ts_normalizes_epoch_and_datetime():
    assert _to_ts(1_700_000_000) == 1_700_000_000.0
    assert _to_ts(1_700_000_000_000) == 1_700_000_000.0  # epoch ms -> s
    assert _to_ts(None) > 0


def test_dollars_and_price_normalization():
    assert _dollars(50, cents=True) == 0.50
    assert _dollars("12.5") == 12.5
    assert _dollars(None) == 0.0
    assert _normalize_price(0.55) == 0.55
    assert _normalize_price(55) == 0.55  # 55 cents -> 0.55


class _FakeKalshi:
    async def get_balance(self):
        return {"balance_dollars": 9.81}

    async def get_fills(self, ticker=None, limit=100, cursor=None):
        return {
            "fills": [
                {"side": "yes", "action": "buy", "yes_price": 50, "count": 2,
                 "total_fee": 1, "created_time": 1_700_000_000},
            ],
            "cursor": None,
        }

    async def get_settlements(self, ticker=None, limit=100):
        return {"settlements": [{"realized_pnl": 40}, {"realized_pnl": -10}]}

    async def get_positions(self, limit=100):
        return {"market_positions": [
            {"ticker": "KXBTC15M-X", "market_title": "BTC up in 15m", "yes": 2,
             "no": 0, "floating_pnl": 10},
        ]}


def test_poll_kalshi_normalizes_real_data():
    snap = asyncio.run(poll_kalshi(_FakeKalshi()))
    assert snap.balance == 9.81
    assert snap.venue_stats["trades"] == 1
    assert snap.venue_stats["notional_usd"] == 0.50 * 2
    assert snap.venue_stats["pnl_usd"] == 0.40 - 0.10
    assert snap.venue_stats["wins"] == 1 and snap.venue_stats["losses"] == 1
    assert snap.trade_totals["total_fees_usd"] == 0.01
    assert snap.recent_trades[0]["side"] == "up"
    assert snap.recent_trades[0]["price"] == 0.50
    assert snap.open_positions[0]["floating_pnl"] == 0.10


class _FakePage:
    def __init__(self, items):
        self.items = tuple(items)


class _FakePaginator:
    def __init__(self, items):
        self._items = items

    async def first_page(self):
        return _FakePage(self._items)


class _FakePosition:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakePoly:
    async def get_balance_allowance(self, asset_type=None):
        return _FakePosition(balance=100.0)

    def list_positions(self, user=None, status=None, page_size=100):
        if status == "OPEN":
            return _FakePaginator([
                _FakePosition(title="BTC up/down", slug="btc-updown-15m", outcome="Yes",
                              current_size=5.0, avg_price=0.5, current_value=2.5,
                              entry_cost_usdc=2.0, unrealized_pnl=0.5),
            ])
        return _FakePaginator([
            _FakePosition(title="BTC up/down", outcome="Yes", realized_pnl=0.80),
            _FakePosition(title="BTC up/down", outcome="No", realized_pnl=-0.30),
        ])

    def list_account_trades(self):
        return _FakePaginator([
            _FakePosition(side="BUY", price=0.5, size=5.0, outcome="Yes", matched_at=1_700_000_000),
        ])


def test_poll_polymarket_normalizes_real_data():
    snap = asyncio.run(poll_polymarket(_FakePoly(), wallet="0xabc"))
    assert snap.balance == 100.0
    assert snap.venue_stats["pnl_usd"] == 0.80 - 0.30
    assert snap.venue_stats["wins"] == 1 and snap.venue_stats["losses"] == 1
    assert snap.recent_trades[0]["side"] == "up"
    assert snap.recent_trades[0]["action"] == "buy"
    assert snap.open_positions[0]["size"] == 5.0
    assert snap.open_positions[0]["avg_price"] == 0.5
