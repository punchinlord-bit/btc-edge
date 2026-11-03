"""Tests for the Command Deck watchdog (ops monitor)."""

from __future__ import annotations

from btc_edge.execution.ops_monitor import (
    DEFAULTS,
    OpsMonitor,
    grade_round,
    run_preflight,
)


def _mono() -> tuple[OpsMonitor, float]:
    clock = [1000.0]

    def now() -> float:
        return clock[0]

    return OpsMonitor(now=now), clock


def test_unknown_before_any_beat():
    m, _ = _mono()
    assert m.status_of("kalshi-feed") == "UNKNOWN"
    assert m.status_of("brain") == "UNKNOWN"


def test_beat_makes_component_alive():
    m, _ = _mono()
    m.beat("kalshi-feed", {"progress": 1})
    assert m.status_of("kalshi-feed") == "ALIVE"


def test_stale_then_dead():
    m, clock = _mono()
    m.beat("kalshi-feed", {"progress": 1})
    assert m.status_of("kalshi-feed") == "ALIVE"
    # staleSec = 5; DEAD is > 3 * staleSec = 15s.
    clock[0] += 6
    assert m.status_of("kalshi-feed") == "STALE"
    clock[0] += 20
    assert m.status_of("kalshi-feed") == "DEAD"


def test_zombie_when_progress_frozen():
    # shadow-engine idleSec = 1200; use a custom component with a tiny idleSec.
    m, clock = _mono()
    m._defs["test-comp"] = {"label": "t", "emoji": "x", "staleSec": 30, "idleSec": 10}
    m.beat("test-comp", {"progress": 0})
    assert m.status_of("test-comp") == "ALIVE"
    # Keep beating (alive) but freeze progress -> ZOMBIE.
    clock[0] += 6
    m.beat("test-comp", {"progress": 0})
    assert m.status_of("test-comp") == "ALIVE"
    clock[0] += 11
    assert m.status_of("test-comp") == "ZOMBIE"
    # Progress resumes -> clears back to ALIVE.
    m.beat("test-comp", {"progress": 1})
    assert m.status_of("test-comp") == "ALIVE"


def test_record_trade_and_grade():
    m, _ = _mono()
    m.record_trade({"id": "t1", "venue": "kalshi", "mode": "shadow", "side": "UP", "qty": 5, "cents": 40, "ts": 1000})
    assert m.stats("shadow")["open"] == 1
    assert m.grade_trade("t1", {"result": "WIN", "pnl": 3.0})["result"] == "WIN"
    assert m.stats("shadow")["graded"] == 1
    assert m.stats("shadow")["wins"] == 1


def test_record_trade_requires_valid_venue():
    m, _ = _mono()
    try:
        m.record_trade({"id": "x", "venue": "binance", "side": "UP"})
        assert False, "expected TypeError"
    except TypeError:
        pass


def test_snapshot_shape():
    m, _ = _mono()
    m.beat("kalshi-feed", {"progress": 1})
    m.record_trade({"id": "s1", "venue": "polymarket", "mode": "shadow", "side": "DOWN", "qty": 2, "cents": 55, "ts": 1000})
    snap = m.snapshot()
    assert set(snap) == {"serverTimeMs", "components", "alerts", "trades", "stats", "lastTradeAgeSec", "events"}
    assert {c["name"] for c in snap["components"]} == set(DEFAULTS)
    assert snap["stats"]["shadow"]["gateTarget"] == 150
    assert snap["trades"][0]["id"] == "s1"


def test_grade_round_win_and_loss():
    trades = [
        {"id": "a", "side": "UP", "qty": 10, "cents": 60},
        {"id": "b", "side": "DOWN", "qty": 10, "cents": 60},
    ]
    out = grade_round(trades, up=True)
    by_id = {r["id"]: r for r in out}
    assert by_id["a"]["result"] == "WIN" and by_id["a"]["pnl"] > 0
    assert by_id["b"]["result"] == "LOSS" and by_id["b"]["pnl"] < 0


def test_preflight_unwired_required_probe_fails():
    import asyncio

    m, _ = _mono()
    m.beat("kalshi-feed", {"progress": 1})
    # No probes wired -> required probes (creds/api/market/balance/strike) fail.
    result = asyncio.run(run_preflight(m, "kalshi", {}))
    assert result["ready"] is False
    ids = {c["id"]: c for c in result["checks"]}
    assert ids["creds"]["level"] == "fail"
    assert "probe not wired" in ids["creds"]["detail"]
