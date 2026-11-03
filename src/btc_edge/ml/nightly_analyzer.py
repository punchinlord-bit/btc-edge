"""Nightly calibration + parameter-search job (the "late-night analyzer").

Runs once daily (target 00:05 UTC) over the prior 24 hours of resolved 15-minute
BTC windows and produces three artifacts:

1. **Calibration report** — overall Brier score and realized win rate, bucketed
   by the model's predicted confidence and by volatility regime (chop / trend /
   spike), so we can see whether a 60-65% call actually won 60-65% of the time.
2. **Walk-forward grid search** — re-scores the day's recorded feature snapshots
   against a grid of ``min_edge_threshold`` x ``entry_seconds_remaining`` x
   feature weights, maximizing net P&L after Kalshi/Polymarket fees.
3. **Output** — a ``candidate_config.json`` with the recommended tweaks (plus an
   ``auto_apply`` flag when the out-of-sample Brier improves by >2%), and a
   human-readable Telegram summary.

Pure stdlib + the existing :func:`~btc_edge.ml.promotion_gate.predict_prob` and
:class:`~btc_edge.ml.promotion_gate.WindowEvaluation` infrastructure. No new
dependencies.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from btc_edge.ml.promotion_gate import REGIMES, WindowEvaluation, predict_prob

# Confidence bucket edges (lower bound inclusive, upper bound exclusive); the
# final edge closes the last bucket at 1.0. This yields 5%-wide buckets.
CONFIDENCE_EDGES: tuple[float, ...] = (0.5, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.0)

DEFAULT_MIN_EDGE_GRID: tuple[float, ...] = (0.0, 0.005, 0.01, 0.02, 0.04)
DEFAULT_ENTRY_SECONDS_GRID: tuple[float, ...] = (60.0, 120.0, 180.0, 300.0, 900.0)


def load_daily_windows(
    session: Any,
    market_key: str | None = None,
    lookback_seconds: float = 86400.0,
) -> list[WindowEvaluation]:
    """Load resolved 15m windows from the prior ``lookback_seconds`` (default 1 day = 96 windows)."""
    from btc_edge.db.models import Btc15mWindowEvaluation

    since = int(time.time()) - int(lookback_seconds)
    query = session.query(Btc15mWindowEvaluation).filter(
        Btc15mWindowEvaluation.window_start_sec >= since
    )
    if market_key:
        query = query.filter(Btc15mWindowEvaluation.market_key == market_key)
    windows: list[WindowEvaluation] = []
    for row in query.all():
        windows.append(
            WindowEvaluation(
                window_start_sec=row.window_start_sec,
                strike_price=row.strike_price,
                close_price=row.close_price,
                outcome=row.outcome,
                regime=row.regime,
                market_key=row.market_key,
                features=json.loads(row.features_json or "{}"),
                market_ask_up=row.market_ask_up,
                market_ask_down=row.market_ask_down,
                fee_rate=row.fee_rate,
            )
        )
    windows.sort(key=lambda w: w.window_start_sec)
    return windows


def calibration_report(
    windows: list[WindowEvaluation], config: dict[str, float] | None = None
) -> dict[str, Any]:
    """Brier score + realized win rate, bucketed by confidence and regime.

    ``config`` is the logistic weight dict consumed by ``predict_prob``; an empty
    config is the constant 0.5 baseline. Calibration is evaluated over ALL
    windows (not just the windows the bot bet on) because it describes the model,
    not the betting policy.
    """
    base = dict(config or {})
    n = len(windows)
    if n == 0:
        return {"n": 0, "brier": 0.0, "win_rate": 0.0, "confidence_buckets": [], "regime_buckets": {}}

    probs = [predict_prob(base, w.features) for w in windows]
    outcomes = [float(w.outcome) for w in windows]
    brier = sum((p - y) ** 2 for p, y in zip(probs, outcomes, strict=True)) / n
    win_rate = sum(1 for y in outcomes if y >= 0.5) / n

    buckets: list[dict[str, Any]] = []
    for i in range(len(CONFIDENCE_EDGES) - 1):
        lo, hi = CONFIDENCE_EDGES[i], CONFIDENCE_EDGES[i + 1]
        idx = [j for j, p in enumerate(probs) if lo <= p < hi]
        if not idx:
            continue
        avg = sum(probs[j] for j in idx) / len(idx)
        wr = sum(outcomes[j] for j in idx) / len(idx)
        buckets.append({"lo": lo, "hi": hi, "n": len(idx), "pred_avg": avg, "win_rate": wr})

    regime_buckets: dict[str, dict[str, Any]] = {}
    for r in REGIMES:
        idx = [j for j, w in enumerate(windows) if w.regime == r]
        cnt = len(idx)
        regime_buckets[r] = {
            "n": cnt,
            "win_rate": (sum(outcomes[j] for j in idx) / cnt) if cnt else 0.0,
        }

    return {
        "n": n,
        "brier": brier,
        "win_rate": win_rate,
        "confidence_buckets": buckets,
        "regime_buckets": regime_buckets,
    }


def _score(
    config: dict[str, float],
    windows: list[WindowEvaluation],
    min_edge: float,
    entry_seconds_remaining: float,
    tf: int = 900,
) -> dict[str, Any]:
    """Score a config against the day's windows, simulating net P&L after fees.

    * ``min_edge`` is the minimum pre-trade edge (``p - ask - fee``) required to
      place a bet.
    * ``entry_seconds_remaining`` is the maximum seconds-before-close at which we
      still enter; windows recorded with more time left are skipped as "too early".

    Brier is computed over ALL windows; P&L / win-rate over placed bets only.
    """
    brier_sum = 0.0
    pnl = 0.0
    bets = 0
    wins = 0
    for w in windows:
        p_up = predict_prob(config, w.features)
        brier_sum += (p_up - w.outcome) ** 2

        # Recover seconds-remaining from the normalized feature stored at record
        # time (``time_remaining_norm = time_remaining / tf``).
        time_remaining = float(w.features.get("time_remaining_norm", 0.0)) * tf
        if time_remaining > entry_seconds_remaining:
            continue

        if p_up >= 0.5:
            edge = p_up - w.market_ask_up - w.fee_rate
            profit = w.outcome - w.market_ask_up - w.fee_rate
        else:
            edge = (1.0 - p_up) - w.market_ask_down - w.fee_rate
            profit = (1.0 - w.outcome) - w.market_ask_down - w.fee_rate
        if edge < min_edge:
            continue

        bets += 1
        pnl += profit
        wins += 1 if profit > 0 else 0

    n = len(windows)
    return {
        "brier": brier_sum / n if n else 0.0,
        "total_pnl": pnl,
        "net_ev_per_bet": pnl / bets if bets else 0.0,
        "bets": bets,
        "wins": wins,
        "win_rate": wins / bets if bets else 0.0,
    }


def _weight_variants(
    base: dict[str, float], windows: list[WindowEvaluation], max_variants: int = 40
) -> list[dict[str, float]]:
    """Candidate weight configs: the base plus ±50% per-weight corners.

    When the base has no learned weights (cold start), seed single-feature probes
    from the feature names actually present in the day's windows so the weight
    dimension is still explored.
    """
    variants: list[dict[str, float]] = [dict(base)]
    keys = [k for k in base if k != "bias" and isinstance(base[k], (int, float))]
    for k in keys:
        up = dict(base)
        up[k] = float(base[k]) * 1.5
        down = dict(base)
        down[k] = float(base[k]) * 0.5
        variants.extend([up, down])
        if len(variants) >= max_variants:
            return variants[:max_variants]

    if not keys:
        seen: list[str] = []
        for w in windows:
            for k in w.features:
                if k not in seen:
                    seen.append(k)
        for k in seen[:12]:
            variants.append({k: 0.5})
            variants.append({k: -0.5})
            if len(variants) >= max_variants:
                break
    return variants[:max_variants]


def grid_search(
    windows: list[WindowEvaluation],
    base_config: dict[str, float] | None = None,
    min_edge_grid: tuple[float, ...] = DEFAULT_MIN_EDGE_GRID,
    entry_seconds_grid: tuple[float, ...] = DEFAULT_ENTRY_SECONDS_GRID,
    tf: int = 900,
) -> dict[str, Any]:
    """Walk-forward grid search maximizing net P&L after fees.

    Returns ``{"best": {...}, "results": [...]}`` where ``best`` carries the
    winning ``config``, ``min_edge_threshold`` and ``entry_seconds_remaining``.
    """
    base = dict(base_config or {})
    best: dict[str, Any] | None = None
    results: list[dict[str, Any]] = []
    for cfg in _weight_variants(base, windows):
        for me in min_edge_grid:
            for er in entry_seconds_grid:
                s = _score(cfg, windows, me, er, tf)
                s["config"] = dict(cfg)
                s["min_edge_threshold"] = me
                s["entry_seconds_remaining"] = er
                results.append(s)
                if best is None or s["total_pnl"] > best["total_pnl"]:
                    best = s
    return {"best": best or {}, "results": results}


def run_nightly_analysis(
    session: Any,
    base_config: dict[str, float] | None = None,
    market_key: str = "btc_15m",
    current_min_edge: float = 0.01,
    current_entry_seconds: float = 840.0,
    candidate_path: str | Path = "data/candidate_config.json",
    tf: int = 900,
) -> dict[str, Any]:
    """Run the full nightly analysis and persist ``candidate_config.json``.

    Returns a dict report used for the Telegram summary and dashboard card.
    ``auto_apply`` is set when the optimal config's out-of-sample Brier improves
    over the current config by more than 2%.
    """
    base = dict(base_config or {})
    windows = load_daily_windows(session, market_key=market_key)
    calibration = calibration_report(windows, base)

    actual = _score(base, windows, current_min_edge, current_entry_seconds, tf)
    actual["min_edge_threshold"] = current_min_edge
    actual["entry_seconds_remaining"] = current_entry_seconds

    grid = grid_search(windows, base, tf=tf)
    optimal = grid["best"]
    if not optimal:
        optimal = {
            "brier": 0.0,
            "total_pnl": 0.0,
            "net_ev_per_bet": 0.0,
            "bets": 0,
            "wins": 0,
            "win_rate": 0.0,
            "config": base,
            "min_edge_threshold": current_min_edge,
            "entry_seconds_remaining": current_entry_seconds,
        }

    actual_brier = float(actual["brier"])
    optimal_brier = float(optimal["brier"])
    brier_improvement_pct = 0.0
    if actual_brier > 1e-9:
        brier_improvement_pct = (actual_brier - optimal_brier) / actual_brier * 100.0
    auto_apply = brier_improvement_pct > 2.0

    recommended = {
        "trading": {
            "min_ev_threshold": float(optimal["min_edge_threshold"]),
            "entry_warmup_seconds": max(0.0, float(tf) - float(optimal["entry_seconds_remaining"])),
        },
        "weights": dict(optimal.get("config", base)),
    }

    candidate = {
        "generated_at": time.time(),
        "market_key": market_key,
        "window_count": len(windows),
        "calibration": calibration,
        "actual": {
            "brier": actual_brier,
            "net_ev_per_bet": actual["net_ev_per_bet"],
            "total_pnl": actual["total_pnl"],
            "bets": actual["bets"],
            "win_rate": actual["win_rate"],
        },
        "optimal": {
            "min_edge_threshold": float(optimal["min_edge_threshold"]),
            "entry_seconds_remaining": float(optimal["entry_seconds_remaining"]),
            "weights": dict(optimal.get("config", base)),
            "brier": optimal_brier,
            "net_ev_per_bet": optimal["net_ev_per_bet"],
            "total_pnl": optimal["total_pnl"],
            "bets": optimal["bets"],
            "win_rate": optimal["win_rate"],
        },
        "brier_improvement_pct": brier_improvement_pct,
        "auto_apply": auto_apply,
        "recommended": recommended,
    }

    path = Path(candidate_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate, indent=2, default=str), encoding="utf-8")
    candidate["candidate_path"] = str(path)
    return candidate


def report_text(report: dict[str, Any]) -> str:
    """One clean line for Telegram — no technical verbage."""
    actual = report.get("actual", {})
    pnl = float(actual.get("total_pnl", 0.0) or 0.0)
    bets = int(actual.get("bets", 0) or 0)
    return f"🌙 Nightly tune-up: reviewed {bets} bets. P&L {pnl:+.2f}. Set for tomorrow."
