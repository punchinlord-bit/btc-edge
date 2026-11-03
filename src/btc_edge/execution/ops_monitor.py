"""Command Deck watchdog: liveness + readiness + trade log for the 15m desk.

Mirrors the Node.js ``opsMonitor.js`` contract so the dashboard ``OpsWidget`` can
render 7 component tiles, two venue readiness cards, an XP bar, and a live +
shadow trade log. A loop can be "alive" while doing nothing, so every component
may also report a PROGRESS counter; a frozen counter past ``idleSec`` => ZOMBIE.

Read-only by design: never places/cancels orders, moves funds, or changes risk.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "kalshi-feed": {"label": "Kalshi feed", "emoji": "🛰️", "staleSec": 5, "idleSec": 30},
    "polymarket-feed": {"label": "Polymarket feed", "emoji": "📡", "staleSec": 5, "idleSec": 30},
    "kalshi-exec": {"label": "Kalshi exec", "emoji": "⚔️", "staleSec": 30, "idleSec": None},
    "polymarket-exec": {"label": "Polymarket exec", "emoji": "🗡️", "staleSec": 30, "idleSec": None},
    "shadow-engine": {"label": "Shadow engine", "emoji": "👻", "staleSec": 30, "idleSec": 1200},
    # brain/grader only beat at each 15m settlement, so their stale window must be
    # >= 900s or they show DEAD between windows (a false "brain is down" alarm).
    "brain": {"label": "Brain / learning", "emoji": "🧠", "staleSec": 900, "idleSec": 3600},
    "grader": {"label": "Grader", "emoji": "📝", "staleSec": 900, "idleSec": 1500},
}

REQUIRED_PROBES = ("creds", "api", "market", "balance", "strike")
PROBE_LABELS = (
    ("creds", "Credentials"),
    ("api", "Exchange API"),
    ("market", "Open 15m market"),
    ("balance", "Balance"),
    ("strike", "Round target"),
    ("clock", "Clock sync"),
    ("risk", "Risk limits"),
    ("armed", "Armed"),
)

_WARN_STATUSES = {"DEAD", "ZOMBIE", "ERROR"}


def _norm_ts(t: Any, now: float) -> float:
    try:
        t = float(t)
    except (TypeError, ValueError):
        return now
    if not (t > 0):
        return now
    return t / 1000.0 if t > 1e11 else t


def _human_age(s: float) -> str:
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


class OpsMonitor:
    """Liveness + readiness + trade log for the BTC 15m desk."""

    def __init__(
        self,
        *,
        now: Callable[[], float] | None = None,
        components: dict[str, dict[str, Any]] | None = None,
        gate_target: int = 150,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dead: Callable[[str, str], None] | None = None,
    ) -> None:
        self._now = now or time.time
        self._defs = {**DEFAULTS, **(components or {})}
        self._gate_target = gate_target
        self._on_event = on_event
        self._on_dead = on_dead
        self._state: dict[str, dict[str, Any]] = {}
        self._trades: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._down: set[str] = set()

    def _ev(self, type_: str, name: str, detail: str = "") -> None:
        ev = {"ts": self._now(), "type": type_, "name": name, "detail": detail or ""}
        self._events.insert(0, ev)
        if len(self._events) > 60:
            self._events.pop()
        if self._on_event is not None:
            with contextlib.suppress(Exception):
                self._on_event(ev)

    def beat(self, name: str, info: dict[str, Any] | None = None) -> None:
        info = info or {}
        t = self._now()
        s = self._state.get(name)
        if s is None:
            s = {"last": t, "progress": None, "progressAt": t, "crashes": 0, "reported": "ok", "detail": ""}
            self._state[name] = s
        s["last"] = t
        s["reported"] = info.get("status") or "ok"
        if info.get("detail") is not None:
            s["detail"] = str(info["detail"])[:200]
        progress = info.get("progress")
        if progress is None or progress != s.get("progress"):
            s["progressAt"] = t
        if progress is not None:
            s["progress"] = progress

    def crash(self, name: str, err: Exception | str | None = None) -> None:
        t = self._now()
        s = self._state.get(name)
        if s is None:
            s = {"last": t, "progress": None, "progressAt": t, "crashes": 0, "reported": "ok", "detail": ""}
            self._state[name] = s
        s["crashes"] += 1
        s["reported"] = "error"
        detail = err if isinstance(err, str) else (str(err) if err is not None else "")
        s["detail"] = detail[:200]
        self._ev("crash", name, s["detail"])

    def status_of(self, name: str) -> str:
        d = self._defs.get(name) or {"staleSec": 60, "idleSec": None}
        s = self._state.get(name)
        if s is None:
            return "UNKNOWN"
        t = self._now()
        age = t - s["last"]
        if age > d["staleSec"] * 3:
            return "DEAD"
        if d["idleSec"] and (t - s["progressAt"]) > d["idleSec"]:
            return "ZOMBIE"
        if s["reported"] == "error":
            return "ERROR"
        if age > d["staleSec"]:
            return "STALE"
        return "ALIVE"

    def check(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in self._defs:
            status = out[name] = self.status_of(name)
            if status in ("DEAD", "ZOMBIE") and name not in self._down:
                self._down.add(name)
                self._ev(status.lower(), name)
                if self._on_dead is not None:
                    with contextlib.suppress(Exception):
                        self._on_dead(name, status)
            elif status == "ALIVE" and name in self._down:
                self._down.discard(name)
                self._ev("recovered", name)
        return out

    # ---- trades ----
    def record_trade(self, tr: dict[str, Any]) -> dict[str, Any]:
        if not tr or tr.get("id") is None:
            raise TypeError("trade.id required")
        venue = tr.get("venue")
        if venue not in ("kalshi", "polymarket"):
            raise TypeError("venue must be kalshi|polymarket")
        id_ = str(tr["id"])
        prev = self._trades.get(id_) or {}
        rec = {
            **prev,
            "id": id_,
            "venue": venue,
            "mode": "shadow" if tr.get("mode") == "shadow" else "live",
            "side": "DOWN" if str(tr.get("side", "")).upper() == "DOWN" else "UP",
            "qty": float(tr.get("qty") or 0),
            "cents": None if tr.get("cents") is None else float(tr["cents"]),
            "ts": _norm_ts(tr.get("ts"), self._now()),
            "bot": tr.get("bot") or prev.get("bot") or None,
            "status": "cancelled" if tr.get("status") == "cancelled" else "open",
            "result": str(tr["result"]).upper() if tr.get("result") else prev.get("result"),
            "pnl": float(tr["pnl"]) if tr.get("pnl") is not None else prev.get("pnl"),
        }
        self._trades.pop(id_, None)
        self._trades[id_] = rec
        while len(self._trades) > 400:
            self._trades.pop(next(iter(self._trades)))
        return rec

    def grade_trade(self, id_: str, grade: dict[str, Any]) -> dict[str, Any] | None:
        prev = self._trades.get(str(id_))
        if prev is None:
            return None
        return self.record_trade({**prev, "result": grade.get("result"), "pnl": grade.get("pnl")})

    def list_trades(self) -> list[dict[str, Any]]:
        return list(self._trades.values())

    def stats(self, mode: str) -> dict[str, Any]:
        a = [x for x in self._trades.values() if x["mode"] == mode and x["status"] != "cancelled"]
        wins = sum(1 for x in a if x["result"] == "WIN")
        losses = sum(1 for x in a if x["result"] == "LOSS")
        pnl = sum(x.get("pnl") or 0 for x in a)
        return {
            "n": len(a), "open": len(a) - wins - losses, "graded": wins + losses,
            "wins": wins, "losses": losses, "pnl": pnl,
        }

    def last_trade_age_sec(self, venue: str, mode: str) -> float | None:
        best: float | None = None
        for x in self._trades.values():
            if (
                x["venue"] == venue and x["mode"] == mode
                and x["status"] != "cancelled"
                and (best is None or x["ts"] > best)
            ):
                best = x["ts"]
        return None if best is None else self._now() - best

    def snapshot(self) -> dict[str, Any]:
        self.check()
        t = self._now()
        components = []
        for n, d in self._defs.items():
            s = self._state.get(n)
            components.append({
                "name": n, "label": d["label"], "emoji": d["emoji"], "status": self.status_of(n),
                "ageSec": (t - s["last"]) if s else None,
                "progressAgeSec": (t - s["progressAt"]) if s else None,
                "crashes": s["crashes"] if s else 0,
                "detail": s["detail"] if s else "",
            })
        alerts = [
            f"{c['emoji']} {c['label']} {c['status']}"
            for c in components if c["status"] in _WARN_STATUSES
        ]
        trades = sorted(self._trades.values(), key=lambda x: x["ts"], reverse=True)[:40]
        shadow = self.stats("shadow")
        shadow["gateTarget"] = self._gate_target
        return {
            "serverTimeMs": round(t * 1000),
            "components": components,
            "alerts": alerts,
            "trades": trades,
            "stats": {"live": self.stats("live"), "shadow": shadow},
            "lastTradeAgeSec": {
                "kalshi": {
                    "live": self.last_trade_age_sec("kalshi", "live"),
                    "shadow": self.last_trade_age_sec("kalshi", "shadow"),
                },
                "polymarket": {
                    "live": self.last_trade_age_sec("polymarket", "live"),
                    "shadow": self.last_trade_age_sec("polymarket", "shadow"),
                },
            },
            "events": self._events[:12],
        }

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


def grade_round(trades: list[dict[str, Any]], up: bool, fee_rate: float = 0.015) -> list[dict[str, Any]]:
    """Grade one finished round for one venue. ``up`` = settled Up/Yes by that venue's rule."""
    out = []
    for t in trades:
        if t.get("status") == "cancelled":
            continue
        win = (str(t.get("side", "UP")).upper() == "UP") == bool(up)
        q = float(t.get("qty") or 0)
        c = (float(t.get("cents")) if t.get("cents") is not None else 50) / 100.0
        pnl = (q * (1 - c) if win else -q * c) - fee_rate * c * (1 - c) * q
        out.append({"id": t["id"], "result": "WIN" if win else "LOSS", "pnl": pnl})
    return out


async def _call_probe(probe: Any) -> dict[str, Any]:
    result = probe()
    if asyncio.iscoroutine(result):
        result = await result
    return result or {}


async def run_preflight(
    mon: OpsMonitor,
    venue: str,
    probes: dict[str, Any],
    *,
    timeout_ms: float = 4000,
    idle_warn_sec: float = 6 * 3600,
) -> dict[str, Any]:
    """Run the read-only readiness checks for one venue."""
    checks: list[dict[str, Any]] = []

    def add(id_: str, label: str, level: str, detail: str = "") -> None:
        checks.append({"id": id_, "label": label, "level": level, "detail": detail or ""})

    feed = mon.status_of(f"{venue}-feed")
    add("feed", "Price feed", "pass" if feed == "ALIVE" else "fail", feed)
    ex = mon.status_of(f"{venue}-exec")
    add("exec", "Exec loop", "pass" if ex == "ALIVE" else ("warn" if ex == "STALE" else "fail"), ex)
    for id_, label in PROBE_LABELS:
        probe = probes.get(id_)
        if probe is None:
            add(id_, label, "fail" if id_ in REQUIRED_PROBES else "warn", "probe not wired")
            continue
        try:
            r = await asyncio.wait_for(_call_probe(probe), timeout=timeout_ms / 1000.0)
            level = "pass" if r.get("ok") else ("warn" if r.get("warn") else "fail")
            add(id_, label, level, r.get("detail", ""))
        except Exception as exc:  # noqa: BLE001
            add(id_, label, "fail", str(exc))
    age = mon.last_trade_age_sec(venue, "live")
    last_level = "warn" if (age is None or age > idle_warn_sec) else "pass"
    last_detail = "none recorded" if age is None else f"{_human_age(age)} ago"
    add("lastTrade", "Last live trade", last_level, last_detail)
    sh = mon.status_of("shadow-engine")
    add("shadow", "Shadow engine", "pass" if sh == "ALIVE" else "warn", sh)
    ready = not any(c["level"] == "fail" for c in checks)
    return {"venue": venue, "ready": ready, "checkedAt": round(time.time() * 1000), "checks": checks}


async def supervise(
    name: str,
    fn: Callable[[Any], Any],
    *,
    monitor: OpsMonitor | None = None,
    initial_backoff_ms: float = 1000,
    max_backoff_ms: float = 60000,
    healthy_ms: float = 60000,
) -> dict[str, Any]:
    """Wrap a long-running loop so a crash restarts it with backoff."""
    stopped = False
    backoff = initial_backoff_ms / 1000.0
    restarts = 0

    class _Ctx:
        def beat(self, info: dict[str, Any] | None = None) -> None:
            if monitor is not None:
                monitor.beat(name, info)

        def is_stopped(self) -> bool:
            return stopped

    ctx = _Ctx()

    async def _runner() -> None:
        nonlocal backoff, restarts
        while not stopped:
            t0 = time.time()
            try:
                result = fn(ctx)
                if asyncio.iscoroutine(result):
                    await result
                if monitor is not None:
                    monitor.beat(name, {"status": "error", "detail": "loop returned unexpectedly"})
            except Exception as exc:  # noqa: BLE001
                if monitor is not None:
                    monitor.crash(name, exc)
            if stopped:
                break
            if time.time() - t0 > healthy_ms / 1000.0:
                backoff = initial_backoff_ms / 1000.0
            restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff_ms / 1000.0)

    task = asyncio.ensure_future(_runner())

    def stop() -> None:
        nonlocal stopped
        stopped = True
        task.cancel()

    return {"stop": stop, "restarts": lambda: restarts}

