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


async def poll_polymarket(client: Any, wallet: str | None = None, limit: int = 100) -> PortfolioSnapshot:
    """Fetch and normalize the Polymarket account (balance, positions, trades)."""
    snap = PortfolioSnapshot()
    try:
        bal = await client.get_balance_allowance(asset_type="COLLATERAL")
        snap.balance = _dollars(getattr(bal, "balance", None))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket balance poll failed: %s", exc)

    open_positions: list[Any] = []
    closed_positions: list[Any] = []
    try:
        open_positions = list(
            (await client.list_positions(user=wallet, status="OPEN", page_size=limit).first_page()).items
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket open positions poll failed: %s", exc)
    try:
        closed_positions = list(
            (await client.list_positions(user=wallet, status="CLOSED", page_size=limit).first_page()).items
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket closed positions poll failed: %s", exc)

    trades: list[Any] = []
    try:
        trades = list((await client.list_account_trades().first_page()).items)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Polymarket trades poll failed: %s", exc)

    notional = 0.0
    realized = 0.0
    wins = 0
    losses = 0
    for p in closed_positions:
        rp = _dollars(getattr(p, "realized_pnl", None))
        realized += rp
        wins += 1 if rp > 0 else 0
        losses += 1 if rp < 0 else 0
    for p in open_positions:
        notional += _dollars(getattr(p, "entry_cost_usdc", None))

    recent: list[dict[str, Any]] = []
    for t in trades:
        price = _normalize_price(getattr(t, "price", None))
        size = _dollars(getattr(t, "size", None))
        outcome = getattr(t, "outcome", "") or ""
        action = (getattr(t, "side", "") or "").lower()
        side = "up" if outcome.lower() in {"yes", "up", "0"} else "down"
        recent.append({
            "ts": _to_ts(getattr(t, "matched_at", None)),
            "venue": "polymarket",
            "tf": 0,
            "strategy": "",
            "side": side,
            "action": action,
            "price": price,
            "size": size,
            "notional": price * size,
            "fee": 0.0,
            "pnl": 0.0,
        })

    snap.venue_stats = {
        "notional_usd": notional,
        "pnl_usd": realized,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
    }
    snap.trade_totals = {
        "total_trades": float(len(trades)),
        "total_notional_usd": notional,
        "total_pnl_usd": realized,
        "total_fees_usd": 0.0,
    }
    snap.recent_trades = recent

    for p in open_positions:
        snap.open_positions.append({
            "venue": "polymarket",
            "ticker": getattr(p, "slug", "") or getattr(p, "title", "") or "",
            "title": getattr(p, "title", "") or "BTC up/down",
            "outcome": getattr(p, "outcome", ""),
            "size": _dollars(getattr(p, "current_size", None)),
            "avg_price": _normalize_price(getattr(p, "avg_price", None)),
            "current_value": _dollars(getattr(p, "current_value", None)),
            "realized_pnl": _dollars(getattr(p, "realized_pnl", None)),
            "unrealized_pnl": _dollars(getattr(p, "unrealized_pnl", None)),
        })
    return snap


