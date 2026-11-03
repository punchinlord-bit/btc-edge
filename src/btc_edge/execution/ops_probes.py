"""Read-only readiness probes for the Command Deck.

Each probe is an async callable returning ``{"ok": bool, "warn": bool, "detail": str}``.
Probes are strictly read-only: never place/cancel orders, move funds, or change risk.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

ProbeFn = Callable[[], Awaitable[dict[str, Any]]]


def _probe(ok: bool, *, warn: bool = False, detail: str = "") -> dict[str, Any]:
    return {"ok": ok, "warn": warn, "detail": detail}


def _clock_probe() -> dict[str, Any]:
    into = int(time.time()) % 900
    return _probe(True, detail=f"server clock ok ({into}s into 15m window)")


def _risk_probe(settings: Any, engine_state: Any) -> dict[str, Any]:
    if bool(getattr(engine_state, "kill_switch", False)):
        return _probe(False, detail="kill switch is engaged")
    bankroll = getattr(settings.trading, "bankroll", 0.0)
    return _probe(True, detail=f"risk ok (bankroll ${bankroll:,.2f})")


def _armed_probe(engine_state: Any) -> dict[str, Any]:
    armed = not bool(getattr(engine_state, "kill_switch", False))
    return _probe(True, warn=not armed, detail="armed" if armed else "halted (kill switch)")


def _strike_probe(engine_state: Any, venue: str) -> dict[str, Any]:
    spot = (getattr(engine_state, "spot_prices", None) or {}).get(venue)
    strike = getattr(engine_state, "open_strike", None)
    if strike is None:
        return _probe(False, detail="no round target set")
    if spot is not None:
        diff = abs(float(spot) - float(strike))
        return _probe(True, detail=f"strike ${strike:,.2f} (feed Δ ${diff:,.2f})")
    return _probe(False, detail=f"strike ${strike:,.2f} but no {venue} feed")


def build_probes(
    settings: Any,
    engine_state: Any,
    kalshi_client: Any | None = None,
    poly_client: Any | None = None,
) -> dict[str, dict[str, ProbeFn]]:
    def strike(v: str) -> ProbeFn:
        return lambda: _strike_probe(engine_state, v)

    return {
        "kalshi": {
            "creds": _creds_probe_kalshi(kalshi_client),
            "api": _api_probe_kalshi(kalshi_client),
            "market": _market_probe(engine_state, "kalshi"),
            "balance": _balance_probe(engine_state, "kalshi"),
            "strike": strike("kalshi"),
            "clock": _clock_probe,
            "risk": lambda: _risk_probe(settings, engine_state),
            "armed": lambda: _armed_probe(engine_state),
        },
        "polymarket": {
            "creds": _creds_probe_polymarket(poly_client),
            "api": _api_probe_polymarket(poly_client),
            "market": _market_probe(engine_state, "polymarket"),
            "balance": _balance_probe(engine_state, "polymarket"),
            "strike": strike("polymarket"),
            "clock": _clock_probe,
            "risk": lambda: _risk_probe(settings, engine_state),
            "armed": lambda: _armed_probe(engine_state),
        },
    }


def _creds_probe_kalshi(client: Any | None) -> ProbeFn:
    async def _run() -> dict[str, Any]:
        if client is None or getattr(client, "signer", None) is None:
            return _probe(False, detail="Kalshi credentials not configured")
        return _probe(True, detail="Kalshi credentials present")
    return _run


def _api_probe_kalshi(client: Any | None) -> ProbeFn:
    async def _run() -> dict[str, Any]:
        if client is None:
            return _probe(False, detail="Kalshi client not built")
        try:
            await client.get_balance()
            return _probe(True, detail="Kalshi /balance answers")
        except Exception as exc:  # noqa: BLE001
            return _probe(False, detail=f"Kalshi API error: {exc}")
    return _run


def _creds_probe_polymarket(client: Any | None) -> ProbeFn:
    async def _run() -> dict[str, Any]:
        if client is None:
            return _probe(False, detail="Polymarket credentials not configured")
        return _probe(True, detail="Polymarket credentials present")
    return _run


def _api_probe_polymarket(client: Any | None) -> ProbeFn:
    async def _run() -> dict[str, Any]:
        if client is None:
            return _probe(False, detail="Polymarket client not built")
        try:
            await client.get_balance()
            return _probe(True, detail="Polymarket balance answers")
        except Exception as exc:  # noqa: BLE001
            return _probe(False, detail=f"Polymarket API error: {exc}")
    return _run


def _market_probe(engine_state: Any, venue: str) -> ProbeFn:
    async def _run() -> dict[str, Any]:
        if venue == "polymarket":
            pm = (getattr(engine_state, "polymarket_markets", None) or {}).get("15m") or {}
            if pm.get("slug") or pm.get("up_token_id"):
                return _probe(True, detail="open Polymarket 15m market resolved")
            return _probe(False, detail="no open Polymarket 15m market")
        kal = getattr(engine_state, "kalshi_market", None) or {}
        if kal.get("ticker"):
            return _probe(True, detail=f"open Kalshi market {kal.get('ticker')}")
        return _probe(False, detail="no open Kalshi 15m market")
    return _run


def _balance_probe(engine_state: Any, venue: str) -> ProbeFn:
    async def _run() -> dict[str, Any]:
        bal = (getattr(engine_state, "wallet_balances", None) or {}).get(venue)
        if bal is None:
            return _probe(False, detail=f"{venue} balance unknown")
        if bal <= 0:
            return _probe(False, detail=f"{venue} balance ${bal:,.2f} (empty)")
        return _probe(True, detail=f"{venue} balance ${bal:,.2f}")
    return _run
