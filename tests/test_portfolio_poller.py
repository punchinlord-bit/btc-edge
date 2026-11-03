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
        # Kalshi returns "balance" (available cash) and "portfolio_value"
        # (open positions), both in integer cents.
        return {"balance": 981, "portfolio_value": 200}  # $9.81 cash + $2.00 positions

    async def get_fills(self, ticker=None, limit=100, cursor=None):
        return {
            "fills": [
                {"side": "yes", "action": "buy", "yes_price": 50, "count": 2,
                 "total_fee": 1, "created_time": 1_700_000_000},
            ],
            "cursor": None,
        }

    async def get_settlements(self, ticker=None, limit=100):
        # Kalshi /portfolio/settlements carries revenue (payout) + per-side cost
        # basis, NOT realized_pnl (which is only in /portfolio/fills).
        return {
            "settlements": [
                {"revenue": 100, "yes_total_cost": 60, "no_total_cost": 0},  # +40c
                {"revenue": 0, "yes_total_cost": 10, "no_total_cost": 0},  # -10c
            ],
        }

    async def get_positions(self, limit=100):
        return {"market_positions": [
            {"ticker": "KXBTC15M-X", "market_title": "BTC up in 15m", "yes": 2,
             "no": 0, "floating_pnl": 10},
        ]}


def test_poll_kalshi_normalizes_real_data():
    snap = asyncio.run(poll_kalshi(_FakeKalshi()))
    assert snap.balance == 9.81
    assert snap.equity_usd == 9.81 + 2.00
    assert snap.venue_stats["trades"] == 1
    assert snap.venue_stats["notional_usd"] == 0.50 * 2
    assert snap.venue_stats["pnl_usd"] == 0.40 - 0.10
    assert snap.venue_stats["wins"] == 1 and snap.venue_stats["losses"] == 1
    assert snap.trade_totals["total_fees_usd"] == 0.01
    assert snap.recent_trades[0]["side"] == "up"
    assert snap.recent_trades[0]["price"] == 0.50
    assert snap.open_positions[0]["floating_pnl"] == 0.10


class _FakePoly:
    async def get_balance(self):
        return 100.0

    async def get_positions(self):
        return [
            {"marketSlug": "cpc-btc-updown-15m-x", "netPosition": "5",
             "cost": {"value": "0.5", "currency": "USD"},
             "realized": {"value": "0.0", "currency": "USD"}},
        ]

    async def get_activities(self):
        def _res(net, pnl):
            return {
                "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
                "positionResolution": {
                    "beforePosition": {"netPosition": net, "cost": {"value": "0.5", "currency": "USD"}},
                    "afterPosition": {"realized": {"value": pnl, "currency": "USD"}},
                    "updateTime": 1_700_000_000,
                },
            }

        return [
            _res("5", "0.80"),
            _res("-5", "-0.30"),
            {"type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "accountBalanceChange": {}},
        ]


def test_poll_polymarket_normalizes_real_data():
    snap = asyncio.run(poll_polymarket(_FakePoly()))
    assert snap.balance == 100.0
    assert snap.venue_stats["pnl_usd"] == 0.80 - 0.30
    assert snap.venue_stats["wins"] == 1 and snap.venue_stats["losses"] == 1
    assert snap.recent_trades[0]["side"] == "up"
    assert snap.open_positions[0]["size"] == 5.0
    assert snap.open_positions[0]["avg_price"] == 0.5
