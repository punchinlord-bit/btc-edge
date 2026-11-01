"""FastAPI web dashboard: live state, kill switch, AI assistant, and health check."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from btc_edge.config import get_settings
from btc_edge.execution.alerts import TelegramAlerter, build_alerter
from btc_edge.execution.audit import AuditLog
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.ml.ai_assistant import AIAssistant, build_assistant
from btc_edge.ml.strategy_registry import Registry

app = FastAPI(title="BTC-Edge Dashboard")


@app.middleware("http")
async def _no_cache_assets(request: Request, call_next):
    """Never let the browser serve a stale dashboard/chart/API payload (this has
    bitten us repeatedly — users see the old panel after a deploy). No-cache
    EVERYTHING, not just HTML, so the iframe rockets and their telemetry JSON
    always revalidate."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Mount static assets only if bundled; the dashboard must never crash on a
# missing favicon/banner (they're cosmetic — the API + health check are core).
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def require_auth(x_api_key: str | None = Header(default=None)) -> None:
    """Require a shared API key on protected endpoints.

    Reads ``DASHBOARD_API_KEY`` from the process environment first (so it can be
    set at deploy time, e.g. Render env vars), then falls back to the value in
    ``.env`` via :class:`Settings`. When the key is unset (local development)
    auth is skipped; in production it MUST be set.
    """
    expected = os.getenv("DASHBOARD_API_KEY", "") or _settings.dashboard_api_key
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


class EngineState:
    """In-memory live engine state shared with the dashboard."""

    def __init__(self) -> None:
        self.mode: str = "paper"
        self.kill_switch: bool = False
        self.window_countdowns: dict[str, float] = {}
        self.brti_60s: float | None = None
        self.open_strike: float | None = None
        self.regime: str = "UNKNOWN"
        self.strategy_metrics: dict[str, dict[str, Any]] = {}
        self.equity_curve: list[tuple[float, float]] = []
        self.news_sentiment: float = 0.0
        self.risk: dict[str, float] = {}
        self.signals: list[str] = []
        self.news_headlines: list[dict[str, Any]] = []
        self.btc_price: float | None = None
        self.price_history: list[tuple[float, float]] = []
        self.polymarket_markets: dict[str, dict[str, Any]] = {}
        self.kalshi_market: dict[str, Any] = {}
        # Our model's own up-probability per venue (the "ahead" reading vs market).
        self.model_prob: dict[str, float] = {}
        self.feed_updated_at: float = 0.0
        self.recent_trades: list[dict[str, Any]] = []
        self.trade_totals: dict[str, float] = {}
        self.venue_stats: dict[str, dict[str, Any]] = {}
        self.wallet_balances: dict[str, float | None] = {"kalshi": None, "polymarket": None}
        self.open_positions: list[dict[str, Any]] = []
        self.updated_at: float = time.time()

    def snapshot(self) -> dict[str, Any]:
        self.updated_at = time.time()
        return {
            "mode": self.mode,
            "kill_switch": self.kill_switch,
            "window_countdowns": self.window_countdowns,
            "brti_60s": self.brti_60s,
            "open_strike": self.open_strike,
            "regime": self.regime,
            "strategy_metrics": self.strategy_metrics,
            "equity_curve": self.equity_curve,
            "news_sentiment": self.news_sentiment,
            "risk": self.risk,
            "signals": self.signals,
            "news_headlines": self.news_headlines,
            "btc_price": self.btc_price,
            "price_history": self.price_history,
            "polymarket_markets": self.polymarket_markets,
            "kalshi_market": self.kalshi_market,
            "model_prob": self.model_prob,
            "recent_trades": self.recent_trades,
            "trade_totals": self.trade_totals,
            "venue_stats": self.venue_stats,
            "wallet_balances": self.wallet_balances,
            "open_positions": self.open_positions,
            "feed_updated_at": self.feed_updated_at,
            "updated_at": self.updated_at,
        }


engine_state = EngineState()

# Shared services (no network on import).
_settings = get_settings()
_audit = AuditLog()
_killswitch = KillSwitch(_settings.safety.kill_switch_file)
_alerter: TelegramAlerter = build_alerter(_settings)
_assistant: AIAssistant = build_assistant(_settings)
_registry: Registry = Registry()
_boot_ts = time.time()

# ---- Champion/Challenger gate + operator readiness (booleans only, no secrets) ----
_OPERATORS = [
    {
        "id": "op1",
        "label": "Operator 1 (Self)",
        "kalshi": "OP1_KALSHI_KEY_ID",
        "poly": "OP1_POLY_WALLET",
        "armed": False,
    },
    {
        "id": "op2",
        "label": "Operator 2 (Partner)",
        "kalshi": "OP2_KALSHI_KEY_ID",
        "poly": "OP2_POLY_WALLET",
        "armed": False,
    },
]


def _operators_view(live: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Operator readiness as booleans only — never expose key material."""
    live = live or {}
    signal = "UP" if (live.get("championProbUp") or 0.5) >= 0.5 else "DOWN"
    out: list[dict[str, Any]] = []
    for o in _OPERATORS:
        out.append(
            {
                "id": o["id"],
                "label": o["label"],
                "armed": o["armed"],
                "kalshiReady": bool(os.getenv(o["kalshi"])),
                "polymarketReady": bool(os.getenv(o["poly"])),
                "signal": signal,
            }
        )
    return out


def _gate_to_chart(result: dict[str, Any] | None) -> dict[str, Any] | None:
    """Translate the Python GateResult into the rocket chart's gate shape."""
    if not result:
        return None
    checks = result.get("checks") or {}
    regimes = result.get("regime_win_rates") or {}
    active = [r for r in regimes if isinstance(regimes.get(r), (int, float))]
    min_regime = min(active, key=lambda r: regimes[r]) if active else None
    min_rate = regimes[min_regime] if min_regime is not None else None
    passed = bool(result.get("passed", False))
    ev_bps = round((result.get("challenger_net_ev", 0.0) - result.get("champion_net_ev", 0.0)) * 10000)
    gate_name = {
        "brier": "brier",
        "net_ev": "ev",
        "regime_floor": "regime",
        "jitter": "jitter",
        "sample_floor": "sample",
    }
    failed = [name for key, name in gate_name.items() if not checks.get(key, True)]
    return {
        "verdict": "PROMOTED" if passed else "REJECTED",
        "failed": failed,
        "gates": {
            "brier": {
                "pass": checks.get("brier", False),
                "challenger": result.get("challenger_brier", 0.0),
                "champion": result.get("champion_brier", 0.0),
            },
            "ev": {"pass": checks.get("net_ev", False), "edgeBps": ev_bps},
            "regime": {"pass": checks.get("regime_floor", False), "min": min_rate, "minRegime": min_regime},
            "jitter": {"pass": checks.get("jitter", False), "retention": result.get("jitter_retention", 0.0)},
        },
    }


def _apply_kill(armed: bool, source: str) -> None:
    engine_state.kill_switch = armed
    if armed:
        _killswitch.arm()
        _audit.record("kill_switch", armed=True, source=source)
        _alerter.send_sync("BTC-Edge: emergency kill switch ENGAGED — trading halted.")
    else:
        _killswitch.reset()
        _audit.record("kill_switch", armed=False, source=source)


@app.get("/api/status", dependencies=[Depends(require_auth)])
def status() -> JSONResponse:
    return JSONResponse(
        {
            "mode": engine_state.mode,
            "kill_switch": engine_state.kill_switch,
            "regime": engine_state.regime,
            "live_allowed": _settings.live_trading_allowed(),
            "trade_only_keys": _settings.safety.trade_only_keys,
        }
    )


@app.get("/api/health")
def health() -> JSONResponse:
    """Liveness/readiness probe for Render (public, minimal output)."""
    return JSONResponse({"status": "ok"})


@app.get("/api/metrics", dependencies=[Depends(require_auth)])
def metrics() -> JSONResponse:
    return JSONResponse(
        {
            "window_countdowns": engine_state.window_countdowns,
            "brti_60s": engine_state.brti_60s,
            "open_strike": engine_state.open_strike,
            "strategy_metrics": engine_state.strategy_metrics,
            "equity_curve": engine_state.equity_curve,
            "btc_price": engine_state.btc_price,
            "price_history": engine_state.price_history,
            "polymarket_markets": engine_state.polymarket_markets,
            "kalshi_market": engine_state.kalshi_market,
            "recent_trades": engine_state.recent_trades,
            "trade_totals": engine_state.trade_totals,
            "venue_stats": engine_state.venue_stats,
            "wallet_balances": engine_state.wallet_balances,
            "open_positions": engine_state.open_positions,
            "risk": engine_state.risk,
            "feed_updated_at": engine_state.feed_updated_at,
        }
    )


@app.get("/api/overview", dependencies=[Depends(require_auth)])
def overview() -> JSONResponse:
    """Single plain-English snapshot for a layperson."""
    sentiment = engine_state.news_sentiment
    tone = "bullish" if sentiment > 0.15 else ("bearish" if sentiment < -0.15 else "neutral")
    risk = engine_state.risk
    at_risk = risk.get("at_risk_usd", 0.0)
    totals = engine_state.trade_totals
    return JSONResponse(
        {
            "mode": engine_state.mode,
            "kill_switch": engine_state.kill_switch,
            "regime": engine_state.regime,
            "news_tone": tone,
            "news_sentiment": sentiment,
            "at_risk_usd": at_risk,
            "signals": engine_state.signals,
            "total_trades": totals.get("total_trades", 0),
            "total_notional_usd": totals.get("total_notional_usd", 0.0),
            "total_pnl_usd": totals.get("total_pnl_usd", 0.0),
            "total_fees_usd": totals.get("total_fees_usd", 0.0),
            "advice": _advice_text(sentiment, engine_state.regime, engine_state.kill_switch),
        }
    )


def _advice_text(sentiment: float, regime: str, killed: bool) -> str:
    if killed:
        return "Trading is halted. Press RESUME only if you intend to restart."
    if regime == "AMPLIFYING":
        return "High volatility: the bot is reducing size. Consider waiting to add money."
    if sentiment > 0.15:
        return "Signals lean bullish: the bot may add. Only risk money you can afford to lose."
    if sentiment < -0.15:
        return "Signals lean bearish: the bot is cautious. Consider pulling risk off."
    return "No strong signal. The bot is waiting for a setup."


@app.get("/api/signals", dependencies=[Depends(require_auth)])
def signals() -> JSONResponse:
    return JSONResponse({"signals": engine_state.signals})


@app.get("/api/news", dependencies=[Depends(require_auth)])
def news() -> JSONResponse:
    return JSONResponse(
        {
            "sentiment": engine_state.news_sentiment,
            "headlines": engine_state.news_headlines,
        }
    )


@app.get("/api/btc-15m/telemetry", dependencies=[Depends(require_auth)])
def btc_15m_telemetry(venue: str = "kalshi") -> JSONResponse:
    """Current 15m window state + Champion/Challenger gate scorecard.

    ``venue`` is ``kalshi`` (default) or ``polymarket``; each venue reports its
    own market's implied up-probability so the two rocket widgets diverge.
    """
    telemetry = _registry.telemetry()
    now = int(time.time())
    start = (now // 900) * 900
    if venue == "polymarket":
        pm = engine_state.polymarket_markets.get("15m", {})
        prob_up = pm.get("prob_up") or pm.get("up")
        strike = pm.get("open_price") or engine_state.open_strike
    else:
        prob_up = engine_state.kalshi_market.get("prob_up")
        strike = engine_state.open_strike
    live = {
        "windowStartSec": start,
        "windowEndSec": start + 900,
        "strikePrice": strike,
        "currentPrice": engine_state.btc_price,
        "championVersionId": telemetry["champion_version_id"],
        "championProbUp": prob_up,
        "challengerProbUp": None,
        "modelProbUp": engine_state.model_prob.get(venue),
        "orderbookImbalance": engine_state.brti_60s,
        "latestNewsSignal": engine_state.news_sentiment,
    }
    gate = _gate_to_chart(_registry.last_result or telemetry.get("last_result"))
    return JSONResponse(
        {
            **live,
            "gate": gate,
            "operators": _operators_view(live),
            "serverTime": now,
            # snake_case aliases kept for the existing dashboard.
            "window_start_sec": start,
            "window_end_sec": start + 900,
            "strike_price": strike,
            "current_price": engine_state.btc_price,
            "champion_version_id": telemetry["champion_version_id"],
            "champion_prob_up": prob_up,
            "orderbook_imbalance": engine_state.brti_60s,
            "latest_news_signal": engine_state.news_sentiment,
            "oos_window_count": telemetry["oos_window_count"],
            "last_result": telemetry["last_result"],
        }
    )


@app.post("/api/btc-15m/operators/{op_id}/arm", dependencies=[Depends(require_auth)])
async def btc_15m_operator_arm(op_id: str, request: Request) -> JSONResponse:
    """Arm/disarm an operator (booleans only — never returns keys)."""
    op = next((o for o in _OPERATORS if o["id"] == op_id), None)
    if op is None:
        return JSONResponse({"error": "unknown operator"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - empty body is fine
        payload = {}
    op["armed"] = bool(payload.get("armed", False))
    return JSONResponse({"id": op_id, "armed": op["armed"]})


@app.get("/rocket", response_class=HTMLResponse)
def rocket() -> HTMLResponse:
    path = TEMPLATE_DIR / "rocketChart.html"
    if not path.exists():
        return HTMLResponse("<html><body><h1>Rocket Chart</h1><p>Template missing.</p></body></html>")
    return HTMLResponse(
        path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.post("/api/assistant", dependencies=[Depends(require_auth)])
async def assistant() -> JSONResponse:
    reply = await _assistant.explain(engine_state.snapshot())
    return JSONResponse({"text": reply.text, "backend": reply.backend})


@app.post("/api/start", dependencies=[Depends(require_auth)])
def start() -> JSONResponse:
    _apply_kill(False, "dashboard")
    return JSONResponse({"kill_switch": False, "message": "Engine resumed"})


@app.post("/api/halt", dependencies=[Depends(require_auth)])
def halt() -> JSONResponse:
    _apply_kill(True, "dashboard")
    return JSONResponse({"kill_switch": True, "message": "Emergency kill switch engaged"})


@app.post("/api/kill-switch", dependencies=[Depends(require_auth)])
def kill_switch() -> JSONResponse:
    return halt()


@app.post("/api/kill-switch/reset", dependencies=[Depends(require_auth)])
def kill_switch_reset() -> JSONResponse:
    return start()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    path = TEMPLATE_DIR / "index.html"
    if not path.exists():
        return HTMLResponse("<html><body><h1>BTC-Edge</h1><p>Dashboard template missing.</p></body></html>")
    return HTMLResponse(
        path.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    icon = STATIC_DIR / "favicon.ico"
    if not icon.exists():
        return FileResponse(icon, status_code=404)
    return FileResponse(icon)


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket, x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("DASHBOARD_API_KEY", "") or _settings.dashboard_api_key
    if expected and x_api_key != expected:
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    try:
        while True:
            # Emit a rocket-chart tick frame ({"type":"tick","price":number}) alongside
            # the full dashboard snapshot.
            price = engine_state.btc_price
            if price is not None:
                await ws.send_json({"type": "tick", "price": price})
            await ws.send_json(engine_state.snapshot())
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
