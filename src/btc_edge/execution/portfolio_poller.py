"""Real portfolio poller.

Fetches *actual* account data — balances, filled orders, settled positions, and
open positions — from Kalshi and Polymarket, and normalizes it into the
dashboard-facing ``venue_stats`` / ``recent_trades`` / ``trade_totals`` /
``open_positions`` shapes so the UI shows only real money, never simulated
numbers.

Each ``poll_*`` function returns a :class:`PortfolioSnapshot` for a single
venue. The caller merges the two snapshots into the shared ``EngineState``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PortfolioSnapshot:
    balance: float | None = None
    venue_stats: dict[str, Any] = field(default_factory=dict)
    recent_trades: list[dict[str, Any]] = field(default_factory=list)
    trade_totals: dict[str, float] = field(default_factory=dict)
    open_positions: list[dict[str, Any]] = field(default_factory=list)


def _to_ts(value: Any) -> float:
    """Normalize an exchange timestamp (epoch s, epoch ms, or datetime) to seconds."""
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value) / 1000.0 if float(value) > 1e12 else float(value)
    try:
        return float(value.timestamp())
    except (AttributeError, ValueError, TypeError):
        return time.time()


def _dollars(value: Any, cents: bool = False) -> float:
    """Parse a numeric money field. ``cents`` means the source is integer cents."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v / 100.0 if cents else v


def _normalize_price(value: Any) -> float:
    """Normalize a contract price to a 0..1 dollar fraction."""
    p = _dollars(value)
    return p / 100.0 if p > 1.5 else p


async def poll_kalshi(client: Any, ticker: str | None = None, limit: int = 200) -> PortfolioSnapshot:
    """Fetch and normalize the Kalshi account (balance, fills, settlements, positions)."""
    snap = PortfolioSnapshot()
    try:
        data = await client.get_balance()
        dollars = data.get("balance_dollars")
        if dollars is not None:
            snap.balance = _dollars(dollars)
        else:
            pv = data.get("portfolio_value") or data.get("balance")
            snap.balance = _dollars(pv, cents=True) if pv is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kalshi balance poll failed: %s", exc)

    # Filled orders (paginate via cursor).
    fills: list[dict[str, Any]] = []
    cursor: str | None = None
    try:
        while len(fills) < limit:
            page = await client.get_fills(ticker=ticker or None, limit=min(limit, 500), cursor=cursor)
            batch = page.get("fills", [])
            fills.extend(batch)
            cursor = page.get("cursor")
            if not cursor or not batch:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kalshi fills poll failed: %s", exc)

    settlements: list[dict[str, Any]] = []
    try:
        sdata = await client.get_settlements(ticker=ticker or None, limit=min(limit, 500))
        settlements = sdata.get("settlements", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kalshi settlements poll failed: %s", exc)

    positions: list[dict[str, Any]] = []
    try:
        pdata = await client.get_positions(limit=min(limit, 200))
        positions = pdata.get("market_positions") or pdata.get("positions") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kalshi positions poll failed: %s", exc)

    notional = 0.0
    fees = 0.0
    recent: list[dict[str, Any]] = []
    for f in fills:
        yes_price = _dollars(f.get("yes_price"), cents=True)
        count = _dollars(f.get("count"))
        fee = _dollars(f.get("total_fee"), cents=True)
        notional += yes_price * count
        fees += fee
        side = "up" if (f.get("side") or "yes").lower() == "yes" else "down"
        recent.append({
            "ts": _to_ts(f.get("created_time")),
            "venue": "kalshi",
            "tf": 900,
            "strategy": "",
            "side": side,
            "action": f.get("action") or "buy",
            "price": yes_price,
            "size": count,
            "notional": yes_price * count,
            "fee": fee,
            "pnl": 0.0,
        })

    realized = 0.0
    wins = 0
    losses = 0
    for s in settlements:
        pnl = _dollars(s.get("realized_pnl"), cents=True)
        realized += pnl
        wins += 1 if pnl > 0 else 0
        losses += 1 if pnl < 0 else 0

    snap.venue_stats = {
        "notional_usd": notional,
        "pnl_usd": realized,
        "trades": len(fills),
        "wins": wins,
        "losses": losses,
    }
    snap.trade_totals = {
        "total_trades": float(len(fills)),
        "total_notional_usd": notional,
        "total_pnl_usd": realized,
        "total_fees_usd": fees,
    }
    snap.recent_trades = recent

    for p in positions:
        snap.open_positions.append({
            "venue": "kalshi",
            "ticker": p.get("ticker") or "",
            "title": p.get("market_title") or "KXBTC15M",
            "yes": _dollars(p.get("yes")),
            "no": _dollars(p.get("no")),
            "floating_pnl": _dollars(p.get("floating_pnl"), cents=True),
        })
    return snap


def _amount_value(value: Any) -> float:
    """Extract a numeric USD amount from a Polymarket US ``Amount`` dict."""
    if isinstance(value, dict):
        return _dollars(value.get("value"))
    return _dollars(value)


async def poll_polymarket(client: Any, limit: int = 100) -> PortfolioSnapshot:
    """Fetch and normalize the Polymarket US account (balance, positions, activity)."""
    snap = PortfolioSnapshot()
    try:
        snap.balance = await client.get_balance()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket balance poll failed: %s", exc)

    positions: list[dict[str, Any]] = []
    try:
        positions = await client.get_positions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket positions poll failed: %s", exc)

    activities: list[dict[str, Any]] = []
    try:
        activities = await client.get_activities()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket activities poll failed: %s", exc)

    notional = 0.0
    realized = 0.0
    for p in positions[:limit]:
        size = _dollars(p.get("netPosition"))
        notional += abs(size)
        snap.open_positions.append({
            "venue": "polymarket",
            "ticker": p.get("marketSlug", ""),
            "title": p.get("marketSlug", "BTC up/down"),
            "outcome": "up" if size >= 0 else "down",
            "size": abs(size),
            "avg_price": _normalize_price(_amount_value(p.get("cost"))),
            "current_value": _amount_value(p.get("marketValue")),
            "realized_pnl": _amount_value(p.get("realized")),
            "unrealized_pnl": 0.0,
        })

    recent: list[dict[str, Any]] = []
    for a in activities:
        typ = a.get("type", "")
        if typ != "ACTIVITY_TYPE_POSITION_RESOLUTION":
            continue
        pr = a.get("positionResolution") or {}
        before = pr.get("beforePosition") or {}
        after = pr.get("afterPosition") or {}
        rp = _amount_value(after.get("realized"))
        realized += rp
        net = _dollars(before.get("netPosition"))
        recent.append({
            "ts": _to_ts(pr.get("updateTime") or pr.get("createTime") or a.get("createTime")),
            "venue": "polymarket",
            "tf": 900,
            "strategy": "",
            "side": "up" if net >= 0 else "down",
            "action": "buy",
            "price": _normalize_price(_amount_value(before.get("cost"))),
            "size": abs(net),
            "notional": abs(net),
            "fee": 0.0,
            "pnl": rp,
        })

    snap.venue_stats = {
        "notional_usd": notional,
        "pnl_usd": realized,
        "trades": len(recent),
        "wins": sum(1 for t in recent if t["pnl"] > 0),
        "losses": sum(1 for t in recent if t["pnl"] < 0),
    }
    snap.trade_totals = {
        "total_trades": float(len(recent)),
        "total_notional_usd": notional,
        "total_pnl_usd": realized,
        "total_fees_usd": 0.0,
    }
    snap.recent_trades = recent
    return snap


