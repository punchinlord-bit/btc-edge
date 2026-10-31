"""Forward-test evaluation for a live prediction log.

Imports a prior run's (timestamp, predicted_probability, actual_outcome) triples
— e.g. your 4-week live "15-minute" prediction log — and computes the metrics
that actually matter: net EV after cost, win rate, Brier score, and Sharpe. This
is real forward-test data, worth more than any synthetic backtest.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Prediction:
    ts: float
    prob: float          # predicted P("up")
    outcome: float       # 1.0 if "up" resolved, 0.0 otherwise


@dataclass(slots=True)
class ForwardTestReport:
    n: int
    net_ev: float        # mean pnl per $1 contract (prob -> outcome)
    win_rate: float
    brier_score: float
    sharpe: float
    total_pnl: float

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "net_ev": round(self.net_ev, 6),
            "win_rate": round(self.win_rate, 6),
            "brier_score": round(self.brier_score, 6),
            "sharpe": round(self.sharpe, 6),
            "total_pnl": round(self.total_pnl, 6),
        }


def compute_metrics(predictions: list[Prediction]) -> ForwardTestReport:
    """Treat each prediction as buying 1 "up" contract at ``prob``, settling at
    ``outcome``. PnL per prediction = outcome - prob."""
    n = len(predictions)
    if n == 0:
        return ForwardTestReport(n=0, net_ev=0.0, win_rate=0.0, brier_score=0.0, sharpe=0.0, total_pnl=0.0)

    pnls = [p.outcome - p.prob for p in predictions]
    total_pnl = sum(pnls)
    net_ev = total_pnl / n
    wins = sum(1 for p in predictions if p.outcome >= 0.5)
    win_rate = wins / n
    brier = sum((p.prob - p.outcome) ** 2 for p in predictions) / n

    mean = net_ev
    var = sum((x - mean) ** 2 for x in pnls) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std * math.sqrt(n)) if std > 0 else 0.0

    return ForwardTestReport(
        n=n, net_ev=net_ev, win_rate=win_rate, brier_score=brier, sharpe=sharpe, total_pnl=total_pnl,
    )


def load_csv(
    path: str | Path,
    ts_col: str = "timestamp",
    prob_col: str = "prob",
    outcome_col: str = "outcome",
) -> list[Prediction]:
    preds: list[Prediction] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                preds.append(Prediction(
                    ts=float(row[ts_col]),
                    prob=float(row[prob_col]),
                    outcome=float(row[outcome_col]),
                ))
            except (KeyError, ValueError):
                continue
    return preds


def load_jsonl(path: str | Path) -> list[Prediction]:
    preds: list[Prediction] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        try:
            preds.append(Prediction(
                ts=float(d.get("timestamp", 0)),
                prob=float(d.get("prob", d.get("predicted_prob", 0))),
                outcome=float(d.get("outcome", d.get("actual", 0))),
            ))
        except (TypeError, ValueError):
            continue
    return preds


def load_any(path: str | Path) -> list[Prediction]:
    p = Path(path)
    if p.suffix.lower() in (".jsonl", ".json", ".ndjson"):
        return load_jsonl(p)
    return load_csv(p)


def report_text(report: ForwardTestReport) -> str:
    verdict = "POSITIVE EDGE" if report.net_ev > 0 else ("NEGATIVE EDGE" if report.net_ev < 0 else "NO EDGE")
    return (
        f"Forward-test report over {report.n} predictions\n"
        f"  Net EV / $1 contract: {report.net_ev:+.4f}   ({verdict})\n"
        f"  Win rate:             {report.win_rate:.2%}\n"
        f"  Brier score:          {report.brier_score:.4f}\n"
        f"  Sharpe:               {report.sharpe:.3f}\n"
        f"  Total PnL ($1/contract): {report.total_pnl:+.4f}\n"
    )
