"""Nightly settlement grader and auto-disable gate.

Queries resolved windows over the trailing ``N`` windows per strategy/rule and
computes realized Net EV, Win Rate, Brier Score, and Sharpe. Any strategy or
Pattern Builder rule with ``realized_net_ev < 0`` over the trailing window is
automatically toggled to ``PAPER_ONLY_QUARANTINE`` and only promoted back when
trailing paper EV turns positive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ACTIVE = "ACTIVE"
QUARANTINE = "PAPER_ONLY_QUARANTINE"


@dataclass(slots=True)
class SettlementRecord:
    strategy: str
    rule_id: str
    window_ts: int
    net_ev: float  # realized PnL after fees for this trade
    win: bool
    prob: float
    outcome: float

    @property
    def brier_term(self) -> float:
        return (self.prob - self.outcome) ** 2


@dataclass(slots=True)
class GradeResult:
    strategy: str = ""
    rule_id: str = ""
    n: int = 0
    net_ev: float = 0.0
    win_rate: float = 0.0
    brier_score: float = 0.0
    sharpe: float = 0.0
    status: str = ACTIVE


def _sharpe(net_evs: list[float]) -> float:
    n = len(net_evs)
    if n < 2:
        return 0.0
    mean = sum(net_evs) / n
    var = sum((x - mean) ** 2 for x in net_evs) / (n - 1)
    std = math.sqrt(var)
    if std <= 0:
        return 0.0
    return mean / std * math.sqrt(n)


class NightlyGrader:
    """Grades strategies/rules over a trailing window and quarantines losers."""

    def __init__(self, trailing_window: int = 200) -> None:
        self.trailing_window = trailing_window

    def _group(self, records: list[SettlementRecord]) -> dict[tuple[str, str], list[SettlementRecord]]:
        groups: dict[tuple[str, str], list[SettlementRecord]] = {}
        for r in records:
            key = (r.strategy, r.rule_id)
            groups.setdefault(key, []).append(r)
        return groups

    def grade(self, records: list[SettlementRecord]) -> dict[tuple[str, str], GradeResult]:
        results: dict[tuple[str, str], GradeResult] = {}
        for key, recs in self._group(records).items():
            # Most recent ``trailing_window`` trades (by window_ts).
            recent = sorted(recs, key=lambda r: r.window_ts)[-self.trailing_window:]
            net_evs = [r.net_ev for r in recent]
            mean_ev = sum(net_evs) / len(net_evs) if net_evs else 0.0
            wins = sum(1 for r in recent if r.win)
            brier = sum(r.brier_term for r in recent) / len(recent) if recent else 0.0
            status = QUARANTINE if mean_ev < 0 else ACTIVE
            results[key] = GradeResult(
                strategy=key[0],
                rule_id=key[1],
                n=len(recent),
                net_ev=mean_ev,
                win_rate=wins / len(recent) if recent else 0.0,
                brier_score=brier,
                sharpe=_sharpe(net_evs),
                status=status,
            )
        return results

    def is_quarantined(
        self, results: dict[tuple[str, str], GradeResult], strategy: str, rule_id: str
    ) -> bool:
        r = results.get((strategy, rule_id))
        return r is not None and r.status == QUARANTINE


def grade_from_db(session, trailing_window: int = 200) -> dict[tuple[str, str], GradeResult]:
    """Grade using ``Settlement`` rows persisted in SQLite."""
    from btc_edge.db.models import Settlement

    records: list[SettlementRecord] = []
    for s in session.query(Settlement).all():
        records.append(SettlementRecord(
            strategy=s.strategy,
            rule_id="default",
            window_ts=int(s.timestamp),
            net_ev=s.realized_net_ev,
            win=s.win,
            prob=0.5,
            outcome=1.0 if s.win else 0.0,
        ))
    return NightlyGrader(trailing_window=trailing_window).grade(records)
