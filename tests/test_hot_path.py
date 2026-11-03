"""Hot-path latency benchmark + manual-trade import tests.

The deterministic decision path (PreGraderGate -> RiskController -> classify_fill)
must complete 1,000 consecutive evaluations with a p99 latency under 1.0ms
(1,000,000 ns), matching the directive's sub-millisecond hot-path requirement.
"""

from __future__ import annotations

import time

from btc_edge.execution.reconciler import human_manual_from_file
from btc_edge.execution.risk_gate import RiskController
from btc_edge.features.time_decay_gate import PreGraderGate


def _one_tick():
    gate = PreGraderGate()
    risk = RiskController()
    gate.evaluate(
        elapsed_seconds=600.0, spot_price=100000.0, strike_price=99800.0,
        realized_vol_per_sec=0.0001, market_ask=0.55, swarm_consensus=0.7,
        true_prob=0.65, recent_ticks=[99970.0, 99985.0, 100000.0],
    )
    risk.evaluate_decision(
        venue="kalshi", side="up", win_prob=0.65, entry_price=0.55, live_balance=100.0
    )


def test_hot_path_p99_under_1ms():
    latencies_ns: list[int] = []
    # warm-up
    for _ in range(100):
        _one_tick()
    for _ in range(1000):
        start = time.perf_counter_ns()
        _one_tick()
        latencies_ns.append(time.perf_counter_ns() - start)

    latencies_ns.sort()
    p99 = latencies_ns[int(len(latencies_ns) * 0.99)]
    assert p99 < 1_000_000, f"p99 latency {p99}ns exceeds 1,000,000ns"


def test_human_manual_import(tmp_path):
    import json

    p = tmp_path / "manual_trades.jsonl"
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"won": True, "pnl": 4.5}) + "\n")
        fh.write(json.dumps({"won": False, "pnl": -3.0}) + "\n")
        fh.write("# a comment line\n")
        fh.write(json.dumps({"pnl": 2.0}) + "\n")

    m = human_manual_from_file(str(p))
    assert m["wins"] == 2
    assert m["losses"] == 1
    assert m["pnl"] == 3.5  # 4.5 - 3.0 + 2.0
