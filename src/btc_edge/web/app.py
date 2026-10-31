"""FastAPI web dashboard: live state, kill switch, AI assistant, and health check."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from btc_edge.config import get_settings
from btc_edge.execution.alerts import TelegramAlerter, build_alerter
from btc_edge.execution.audit import AuditLog
from btc_edge.execution.killswitch import KillSwitch
from btc_edge.ml.ai_assistant import AIAssistant, build_assistant

app = FastAPI(title="BTC-Edge Dashboard")

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
            "updated_at": self.updated_at,
        }


engine_state = EngineState()

# Shared services (no network on import).
_settings = get_settings()
_audit = AuditLog()
_killswitch = KillSwitch(_settings.safety.kill_switch_file)
_alerter: TelegramAlerter = build_alerter(_settings)
_assistant: AIAssistant = build_assistant(_settings)
_boot_ts = time.time()


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
    return JSONResponse({
        "mode": engine_state.mode,
        "kill_switch": engine_state.kill_switch,
        "regime": engine_state.regime,
        "live_allowed": _settings.live_trading_allowed(),
        "trade_only_keys": _settings.safety.trade_only_keys,
    })


@app.get("/api/health")
def health() -> JSONResponse:
    """Liveness/readiness probe for Render (public, minimal output)."""
    return JSONResponse({"status": "ok"})


@app.get("/api/metrics", dependencies=[Depends(require_auth)])
def metrics() -> JSONResponse:
    return JSONResponse({
        "window_countdowns": engine_state.window_countdowns,
        "brti_60s": engine_state.brti_60s,
        "open_strike": engine_state.open_strike,
        "strategy_metrics": engine_state.strategy_metrics,
        "equity_curve": engine_state.equity_curve,
    })


@app.get("/api/overview", dependencies=[Depends(require_auth)])
def overview() -> JSONResponse:
    """Single plain-English snapshot for a layperson."""
    sentiment = engine_state.news_sentiment
    tone = "bullish" if sentiment > 0.15 else ("bearish" if sentiment < -0.15 else "neutral")
    risk = engine_state.risk
    at_risk = risk.get("at_risk_usd", 0.0)
    return JSONResponse({
        "mode": engine_state.mode,
        "kill_switch": engine_state.kill_switch,
        "regime": engine_state.regime,
        "news_tone": tone,
        "news_sentiment": sentiment,
        "at_risk_usd": at_risk,
        "signals": engine_state.signals,
        "advice": _advice_text(sentiment, engine_state.regime, engine_state.kill_switch),
    })


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
    return JSONResponse({
        "sentiment": engine_state.news_sentiment,
        "headlines": engine_state.news_headlines,
    })


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
    return HTMLResponse(path.read_text(encoding="utf-8"))


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
            await ws.send_json(engine_state.snapshot())
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


def run(host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
