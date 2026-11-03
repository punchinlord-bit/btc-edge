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
from btc_edge.execution.ops_monitor import OpsMonitor, run_preflight
from btc_edge.ml.ai_assistant import AIAssistant, build_assistant
from btc_edge.ml.strategy_registry import Registry

app = FastAPI(title="BTC-Edge Dashboard")


@app.middleware("http")
async def _no_cache_assets(request: Request, call_next):
    """Hard no-cache on EVERY response so mobile Safari/Chrome never serve a stale
    cached dashboard or /rocket iframe (standard meta tags are ignored on phones)."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "-1"
    response.headers["Surrogate-Control"] = "no-store"
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
        self.mode: str = "live"
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
        # DeepSeek oracle's cached p_up (None until the first successful call).
        self.deepseek_prob: float | None = None
        self.deepseek_reason: str = ""
        # Reconciler's AI_LIVE / AI_SHADOW / HUMAN_MANUAL attribution scoreboards.
        self.attribution: dict[str, dict[str, float]] = {}
        # 20-agent swarm consensus per venue (quorum/dispersion/net-edge telemetry).
        self.swarm: dict[str, dict[str, Any]] = {}
        self.feed_updated_at: float = 0.0
        self.recent_trades: list[dict[str, Any]] = []
        self.trade_totals: dict[str, float] = {}
        self.venue_stats: dict[str, dict[str, Any]] = {}
        self.wallet_balances: dict[str, float | None] = {"kalshi": None, "polymarket": None}
        self.open_positions: list[dict[str, Any]] = []
        self.nightly_analysis: dict[str, Any] = {}
        self.gusto_mode: bool = False
        self.gusto_armed_at: float = 0.0
        self.bot_bets: list[dict[str, Any]] = []
        self.kalshi_auth_status: str = "unknown"
        self.bot_status: dict[str, str] = {}
        self.last_settled_bet: dict[str, Any] | None = None
        # Monotonic counter bumped on every profitable sell/exit. The dashboard
        # watches this to play the "yeaaa" celebration sound on a win.
        self.sell_win_seq: int = 0
        self.spot_prices: dict[str, float | None] = {"kalshi": None, "polymarket": None}
        self.spot_ts: dict[str, float] = {"kalshi": 0.0, "polymarket": 0.0}
        # Local Ollama 7B execution gate status (blinking panel indicator).
        self.ollama_status: dict[str, Any] = {
            "state": "loading",  # loading | ready | offline | disabled
            "model": "",
            "last_verdict": "",
            "last_latency_ms": 0.0,
            "num_verdicts": 0,
            "num_vetoes": 0,
            "num_timeouts": 0,
        }
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
            "nightly_analysis": self.nightly_analysis,
            "gusto_mode": self.gusto_mode,
            "bot_bets": self.bot_bets,
            "kalshi_auth_status": self.kalshi_auth_status,
            "bot_status": self.bot_status,
            "last_settled_bet": self.last_settled_bet,
            "sell_win_seq": self.sell_win_seq,
            "feed_updated_at": self.feed_updated_at,
            "ollama_status": self.ollama_status,
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

# Command Deck watchdog: liveness + readiness + trade log. The read-only probes
# are registered by run_engine.py once the real clients are built.
_ops = OpsMonitor()
_ops_probes: dict[str, dict[str, Any]] = {}
_ops_preflight_cache: dict[str, Any] = {"at": 0.0, "val": None}


def set_ops_probes(probes: dict[str, dict[str, Any]]) -> None:
    """Register the per-venue readiness probes (called once from run_engine.py)."""
    global _ops_probes
    _ops_probes = probes or {}


def get_ops_monitor() -> OpsMonitor:
    return _ops


# Local Ollama gate reference (set by run_engine.py after the trader is built) so
# the dashboard's model picker can list + switch models on the LIVE gate instance.
_ollama_gate: Any = None


def set_ollama_gate(gate: Any) -> None:
    """Register the live Ollama gate (called once from run_engine.py)."""
    global _ollama_gate
    _ollama_gate = gate

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
    # Dedup: only alert/audit on an actual state CHANGE so repeated halt/resume
    # clicks don't spam Telegram with identical "ENGAGED" messages.
    changed = bool(engine_state.kill_switch) != bool(armed)
    engine_state.kill_switch = armed
    if armed:
        _killswitch.arm()
        if changed:
            _audit.record("kill_switch", armed=True, source=source)
            _alerter.send_sync("🛑 Trading paused (safety switch). Resume whenever you're ready.")
    else:
        _killswitch.reset()
        if changed:
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
            "ollama_status": engine_state.ollama_status,
        }
    )


async def _switch_model(gate: Any, model: str) -> None:
    """Background model switch: unload the old model, load + warm the new one."""
    old = getattr(gate, "model", "")
    gate.set_model(model)
    engine_state.ollama_status["model"] = model
    engine_state.ollama_status["state"] = "loading"
    # Reset the dashboard counters immediately (fresh model = fresh stats).
    engine_state.ollama_status["last_verdict"] = ""
    engine_state.ollama_status["num_verdicts"] = 0
    engine_state.ollama_status["num_vetoes"] = 0
    engine_state.ollama_status["num_timeouts"] = 0
    if old and old != model:
        await gate.unload(old)
    ok = await gate.warmup()
    engine_state.ollama_status["state"] = "ready" if ok else "offline"


@app.get("/api/ollama/models", dependencies=[Depends(require_auth)])
async def ollama_models() -> JSONResponse:
    """List installed Ollama models + the currently selected one."""
    models: list[dict[str, Any]] = []
    if _ollama_gate is not None:
        models = await _ollama_gate.list_models()
    return JSONResponse({
        "current": engine_state.ollama_status.get("model", ""),
        "models": models,
    })


@app.post("/api/ollama/switch", dependencies=[Depends(require_auth)])
async def ollama_switch(request: Request) -> JSONResponse:
    """Switch the live gate to a different installed model (re-warms in background)."""
    if _ollama_gate is None:
        return JSONResponse({"error": "Ollama gate not configured"}, status_code=400)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    model = str(payload.get("model") or "").strip()
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)
    asyncio.create_task(_switch_model(_ollama_gate, model))
    return JSONResponse({"ok": True, "model": model, "status": "switching"})


@app.get("/api/health")
def health() -> JSONResponse:
    """Liveness/readiness probe for Render (public, minimal output)."""
    return JSONResponse({"status": "ok"})


@app.get("/api/nightly-analysis", dependencies=[Depends(require_auth)])
def nightly_analysis() -> JSONResponse:
    """Latest late-night analyzer report (calibration + grid search + candidate)."""
    return JSONResponse(engine_state.nightly_analysis or {})


@app.post("/api/gusto-mode", dependencies=[Depends(require_auth)])
async def gusto_mode(request: Request) -> JSONResponse:
    """Arm/disarm aggressive Polymarket "gusto" recovery mode.

    ARMING requires the correct ``GUSTO_MODE_KEYWORD`` (never returned to the
    client). Disarming is always allowed. The mode auto-disarms after the
    configured timeout so it can never leak indefinitely.
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - empty/invalid body is treated as disarm
        payload = {}
    enabled = bool(payload.get("enabled", False))

    if enabled:
        import hmac

        keyword = str(payload.get("keyword") or "")
        expected = os.getenv("GUSTO_MODE_KEYWORD", "") or _settings.gusto_mode_keyword
        if not expected or not hmac.compare_digest(keyword, expected):
            raise HTTPException(status_code=403, detail="Incorrect or missing gusto keyword")
        engine_state.gusto_mode = True
        engine_state.gusto_armed_at = time.time()
    else:
        engine_state.gusto_mode = False
        engine_state.gusto_armed_at = 0.0

    return JSONResponse({"gusto_mode": engine_state.gusto_mode})


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
            "sell_win_seq": engine_state.sell_win_seq,
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
    """Per-venue 15m window state for the rocket widgets + dashboard.

    Returns a ``venues`` map (``kalshi`` + ``polymarket``) so each graph instance
    reads only its own venue's feed + bets, plus ``buildVersion`` for
    cache-busting. Reads in-memory state only — never makes an outbound network
    call — so it returns in well under 5ms.
    """
    from btc_edge.connectors.kalshi_client import _parse_ts
    from btc_edge.version import BUILD_TIMESTAMP, BUILD_VERSION

    telemetry = _registry.telemetry()
    now = time.time()
    start = int(now) // 900 * 900
    spot = engine_state.btc_price

    def _venue_state(v: str) -> dict[str, Any]:
        strike = engine_state.open_strike
        close_ts: float | None = None
        if v == "polymarket":
            pm = engine_state.polymarket_markets.get("15m", {})
            strike = pm.get("open_price") or engine_state.open_strike
            end = pm.get("end_date")
            close_ts = _parse_ts(end) if end else None
        else:
            close_ts = engine_state.kalshi_market.get("close_time")
        # Use the venue's ACTUAL market close time (Kalshi KXBTC15M / Polymarket
        # cpc-btc-updown-15m) so the rocket countdown matches each site exactly,
        # falling back to the UTC-aligned boundary only if the feed is stale.
        window_end = close_ts if close_ts else (start + 900)
        window_start = window_end - 900
        vs = engine_state.venue_stats.get(v, {}) or {}
        bets = [b for b in engine_state.bot_bets if b.get("venue") == v]
        wins = int(vs.get("wins", 0) or 0)
        losses = int(vs.get("losses", 0) or 0)
        net = float(vs.get("pnl_usd", 0.0) or 0.0)
        # The venue's OWN live spot feed (Kalshi synthetic BRTI / Polymarket
        # Chainlink), falling back to the shared Coinbase spot until connected.
        price = engine_state.spot_prices.get(v) or spot
        return {
            "price": price,
            "strike": strike,
            "ts": now,
            "windowStart": window_start,
            "windowEnd": window_end,
            "modelProbUp": engine_state.model_prob.get(v),
            "sigma": 1.45,
            "bets": bets,
            "stats": {
                "balanceUsd": engine_state.wallet_balances.get(v),
                "wins": wins,
                "losses": losses,
                "netPnlUsd": net,
                "equityUsd": vs.get("equity_usd"),
                "balanceSource": vs.get("balance_source", "api"),
            },
            "armed": not engine_state.kill_switch,
            "botStatus": engine_state.bot_status.get(v) or (
                "ARMED · Scanning 15m Edge" if not engine_state.kill_switch else "HALTED"
            ),
            # 20-agent swarm consensus signal (quorum / dispersion / net edge).
            "swarm": engine_state.swarm.get(v),
            "lastSettledBet": engine_state.last_settled_bet
            if (engine_state.last_settled_bet or {}).get("venue") == v
            else None,
        }

    kalshi = _venue_state("kalshi")
    polymarket = _venue_state("polymarket")
    # Ghost comparison line: expose the other venue's price so each graph can
    # render the ~1s lead/lag between feeds.
    kalshi["otherVenuePrice"] = polymarket["price"]
    polymarket["otherVenuePrice"] = kalshi["price"]

    # Live lead/lag: Kalshi's synthetic BRTI leads Polymarket's Chainlink by ~1s,
    # and the dollar spread is the live difference between the two spot feeds.
    price_spread = round((kalshi["price"] or 0.0) - (polymarket["price"] or 0.0), 2)
    lag_ms = int(
        (engine_state.spot_ts.get("kalshi", 0.0) - engine_state.spot_ts.get("polymarket", 0.0)) * 1000
    ) or 1000

    return JSONResponse(
        {
            "buildVersion": BUILD_VERSION,
            "buildTimestamp": BUILD_TIMESTAMP,
            "windowStart": start,
            "windowLength": 900,
            "serverTime": now,
            # Rolling per-venue tick backfill so a chart opened mid-round is full.
            # (Shared Coinbase history is a close proxy for both venues' spot.)
            "priceHistory": [
                {"t": float(ts), "p": float(p)}
                for ts, p in engine_state.price_history[-900:]
            ],
            "venues": {"kalshi": kalshi, "polymarket": polymarket},
            "kalshiAuthStatus": engine_state.kalshi_auth_status,
            "sellWinSeq": engine_state.sell_win_seq,
            "priceSpread": price_spread,
            "lagMs": lag_ms,
            # Flat aliases kept for the existing dashboard + any older widgets.
            "window_start_sec": start,
            "window_end_sec": start + 900,
            "strike_price": kalshi["strike"] if venue == "kalshi" else polymarket["strike"],
            "current_price": spot,
            "model_prob_up": engine_state.model_prob.get(venue),
            "champion_version_id": telemetry["champion_version_id"],
            "oos_window_count": telemetry["oos_window_count"],
            "last_result": telemetry["last_result"],
            "gate": _gate_to_chart(_registry.last_result or telemetry.get("last_result")),
            "operators": _operators_view({"championProbUp": engine_state.model_prob.get(venue)}),
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


def _ops_write_allowed(x_ops_key: str | None) -> bool:
    """Writes (heartbeat/trade) need ``x-ops-key`` when OPS_KEY is set; else localhost."""
    ops_key = os.getenv("OPS_KEY", "")
    if ops_key:
        return bool(x_ops_key) and x_ops_key == ops_key
    return True  # single-process local dashboard: internal beats call ops directly


@app.get("/api/btc-15m/ops")
async def btc_15m_ops() -> JSONResponse:
    """Command Deck snapshot: component tiles + venue readiness + trade log.

    Public (no auth): exposes only booleans and masked/rounded values, never secrets.
    """
    if _ops_preflight_cache["val"] is None or time.time() - _ops_preflight_cache["at"] > 10.0:
        kalshi = await run_preflight(_ops, "kalshi", _ops_probes.get("kalshi", {}))
        polymarket = await run_preflight(_ops, "polymarket", _ops_probes.get("polymarket", {}))
        _ops_preflight_cache["at"] = time.time()
        _ops_preflight_cache["val"] = {"kalshi": kalshi, "polymarket": polymarket}
    snap = _ops.snapshot()
    snap["readiness"] = _ops_preflight_cache["val"]
    return JSONResponse(snap)


@app.post("/api/btc-15m/ops/heartbeat")
async def btc_15m_ops_heartbeat(
    request: Request, x_ops_key: str | None = Header(default=None)
) -> JSONResponse:
    if not _ops_write_allowed(x_ops_key):
        return JSONResponse({"error": "bad x-ops-key"}, status_code=401)
    body = await request.json()
    name = str(body.get("name") or "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    _ops.beat(
        name,
        {"status": body.get("status"), "detail": body.get("detail"), "progress": body.get("progress")},
    )
    return JSONResponse({"ok": True})


@app.post("/api/btc-15m/ops/trade")
async def btc_15m_ops_trade(
    request: Request, x_ops_key: str | None = Header(default=None)
) -> JSONResponse:
    if not _ops_write_allowed(x_ops_key):
        return JSONResponse({"error": "bad x-ops-key"}, status_code=401)
    body = await request.json()
    try:
        rec = _ops.record_trade(body)
        return JSONResponse(rec)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/btc-15m/ops/trade/{trade_id}/grade")
async def btc_15m_ops_grade(
    trade_id: str, request: Request, x_ops_key: str | None = Header(default=None)
) -> JSONResponse:
    if not _ops_write_allowed(x_ops_key):
        return JSONResponse({"error": "bad x-ops-key"}, status_code=401)
    body = await request.json()
    rec = _ops.grade_trade(trade_id, body)
    if rec is None:
        return JSONResponse({"error": "unknown trade id"}, status_code=404)
    return JSONResponse(rec)


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


@app.get("/ops", response_class=HTMLResponse)
def ops_page() -> HTMLResponse:
    """Command Deck widget page (watchdog + readiness + shadow trade log)."""
    path = TEMPLATE_DIR / "opsWidget.html"
    if not path.exists():
        return HTMLResponse("<html><body><h1>Command Deck</h1><p>Template missing.</p></body></html>")
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
