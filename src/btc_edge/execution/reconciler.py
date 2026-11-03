"""Kalshi reconciler: strict AI_LIVE / HUMAN_MANUAL / AI_SHADOW attribution.

Fixes two production problems:

* **Human vs AI collision** — manual Kalshi UI trades were being lumped into the
  bot's win rate and Brier retraining. Every fill is now classified by its
  ``client_order_id``: the bot's orders are prefixed ``AI_v3_``, everything else is
  a human/manual trade.
* **Historical cutoff loss** — settled wins that age past Kalshi's
  ``/historical/cutoff`` and disappear from ``/portfolio/fills`` are recovered by
  also pulling ``/historical/orders`` + ``/historical/fills``.

Produces three separated scoreboards and a Telegram-ready summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AI_PREFIX = "AI_v3_"


@dataclass
class Bucket:
    name: str  # "AI_LIVE" | "HUMAN_MANUAL" | "AI_SHADOW"
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    brier_sum: float = 0.0
    brier_count: int = 0

    @property
    def trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_brier(self) -> float | None:
        return self.brier_sum / self.brier_count if self.brier_count else None


@dataclass
class Scoreboard:
    ai_live: Bucket = field(default_factory=lambda: Bucket("AI_LIVE"))
    ai_shadow: Bucket = field(default_factory=lambda: Bucket("AI_SHADOW"))
    human_manual: Bucket = field(default_factory=lambda: Bucket("HUMAN_MANUAL"))

    def get(self, name: str) -> Bucket:
        for b in (self.ai_live, self.ai_shadow, self.human_manual):
            if b.name == name:
                return b
        raise KeyError(name)


def classify_fill(client_order_id: str | None) -> str:
    """Classify a fill into ``AI_LIVE`` or ``HUMAN_MANUAL`` by its client_order_id."""
    if client_order_id and str(client_order_id).startswith(AI_PREFIX):
        return "AI_LIVE"
    return "HUMAN_MANUAL"


def _dollars(value: Any, cents: bool = False) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v / 100.0 if cents else v


def build_scoreboard(
    fills: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
    shadow_metrics: dict[str, Any] | None = None,
) -> Scoreboard:
    """Build the three separated buckets from fills + settlements + shadow metrics.

    ``fills`` are raw Kalshi fill dicts (each with ``client_order_id``, ``side``,
    ``count`` and ``yes_price`` in cents). ``settlements`` are per-market Kalshi
    settlement dicts carrying ``revenue`` (payout, cents) and the cost basis as
    ``yes_total_cost_dollars`` / ``no_total_cost_dollars``. Human vs AI is separated
    purely by the ``AI_v3_`` prefix on ``client_order_id``.
    """
    sb = Scoreboard()

    # --- AI_SHADOW: from the paper-trading ledger (exact). ---
    if shadow_metrics:
        sb.ai_shadow.wins = int(shadow_metrics.get("wins", 0) or 0)
        sb.ai_shadow.losses = int(shadow_metrics.get("losses", 0) or 0)
        sb.ai_shadow.pnl = float(shadow_metrics.get("shadowPnL", 0.0) or 0.0)
        sb.ai_shadow.brier_sum = float(shadow_metrics.get("brierSum", 0.0) or 0.0)
        sb.ai_shadow.brier_count = int(shadow_metrics.get("count", 0) or 0)

    # --- Aggregate AI vs human notional from fills. ---
    ai_notional = 0.0
    human_notional = 0.0
    for f in fills:
        price = _dollars(f.get("yes_price"), cents=True)
        count = max(0.0, _dollars(f.get("count"), cents=False))
        notional = price * count
        if classify_fill(f.get("client_order_id")) == "AI_LIVE":
            ai_notional += notional
        else:
            human_notional += notional

    # --- Sum each settlement's realized PnL and split by notional share. ---
    total_notional = ai_notional + human_notional
    for s in settlements:
        revenue = _dollars(s.get("revenue"), cents=True)
        yes_cost = s.get("yes_total_cost_dollars")
        no_cost = s.get("no_total_cost_dollars")
        if yes_cost is None and no_cost is None:
            cost = (
                _dollars(s.get("yes_total_cost"), cents=True)
                + _dollars(s.get("no_total_cost"), cents=True)
            )
        else:
            cost = _dollars(yes_cost) + _dollars(no_cost)
        pnl = revenue - cost
        if pnl == 0.0:
            continue
        if total_notional <= 0:
            sb.human_manual.pnl += pnl
            sb.human_manual.wins += 1 if pnl > 0 else 0
            sb.human_manual.losses += 1 if pnl < 0 else 0
            continue
        ai_pnl = pnl * (ai_notional / total_notional)
        human_pnl = pnl - ai_pnl
        if abs(ai_pnl) > 1e-9:
            sb.ai_live.pnl += ai_pnl
            sb.ai_live.wins += 1 if ai_pnl > 0 else 0
            sb.ai_live.losses += 1 if ai_pnl < 0 else 0
        if abs(human_pnl) > 1e-9:
            sb.human_manual.pnl += human_pnl
            sb.human_manual.wins += 1 if human_pnl > 0 else 0
            sb.human_manual.losses += 1 if human_pnl < 0 else 0

    return sb


def format_telegram(sb: Scoreboard) -> str:
    """Render the AI's own W/L into a single plain-English line."""
    b = sb.ai_live
    if not b.trades:
        return "🤖 No trades yet — sitting tight."
    pnl = f"{'+' if b.pnl >= 0 else ''}${b.pnl:.2f}"
    return f"🤖 AI so far: {b.wins}W / {b.losses}L · {pnl}"


def ai_live_from_ledger(
    ledger_path: str = "data/trades.jsonl", venue: str = "kalshi"
) -> dict[str, float]:
    """Compute the AI_LIVE bucket from the bot's own settle ledger.

    The ledger is written locally on every settlement and never ages out (unlike
    Kalshi's live /portfolio endpoints, which drop records past their cutoff), so
    this is the authoritative source for the bot's real W/L and PnL.
    """
    import json
    from pathlib import Path

    wins = 0
    losses = 0
    pnl = 0.0
    path = Path(ledger_path)
    if not path.exists():
        return {"wins": 0, "losses": 0, "pnl": 0.0}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "settle" or rec.get("venue") != venue:
                continue
            p = float(rec.get("pnl", 0.0) or 0.0)
            if p > 0:
                wins += 1
            elif p < 0:
                losses += 1
            pnl += p
    return {"wins": wins, "losses": losses, "pnl": round(pnl, 2)}


def human_manual_from_file(path: str = "data/manual_trades.jsonl") -> dict[str, float]:
    """Read the operator's manually-entered Kalshi UI trades.

    Because Kalshi's live /portfolio endpoints drop records past their cutoff (and
    the /historical endpoints are unavailable on this host), the operator's own UI
    trades are reconciled from a local, never-aging JSONL file. Each line is either
    ``{"won": true, "pnl": 5.0}`` (payout is assumed $1 if ``pnl`` is omitted) or a
    plain ``{"pnl": 5.0}`` (positive = win, negative = loss).
    """
    import json
    from pathlib import Path

    wins = 0
    losses = 0
    pnl = 0.0
    p = Path(path)
    if not p.exists():
        return {"wins": 0, "losses": 0, "pnl": 0.0}
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "pnl" in rec and rec["pnl"] is not None:
                pv = float(rec["pnl"])
            else:
                won = bool(rec.get("won", False))
                pv = float(rec.get("payout", 1.0) if won else (rec.get("cost", 0.0) or -1.0))
            if pv > 0:
                wins += 1
            elif pv < 0:
                losses += 1
            pnl += pv
    return {"wins": wins, "losses": losses, "pnl": round(pnl, 2)}


class KalshiReconciler:
    """Fetches live + historical Kalshi records and builds the attribution report."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def fetch_fills(self, ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        import logging

        log = logging.getLogger("reconciler")
        fills: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(fills) < limit:
            try:
                page = await self.client.get_fills(ticker=ticker, limit=min(limit, 500), cursor=cursor)
            except Exception as exc:  # noqa: BLE001
                log.warning("reconciler live fills failed: %s", exc)
                break
            batch = page.get("fills", []) or []
            fills.extend(batch)
            cursor = page.get("cursor")
            if not cursor or not batch:
                break
        # Historical fills (pre-cutoff) that aged out of /portfolio/fills.
        try:
            hist = await self.client.get_historical_fills(ticker=ticker, limit=min(limit, 500))
            fills.extend(hist.get("fills", []) or [])
        except Exception as exc:  # noqa: BLE001 - historical endpoint may be absent
            log.warning("reconciler historical fills failed: %s", exc)
        return fills

    async def fetch_settlements(self, ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        import logging

        log = logging.getLogger("reconciler")
        settlements: list[dict[str, Any]] = []
        try:
            resp = await self.client.get_settlements(ticker=ticker, limit=min(limit, 500))
            settlements.extend(resp.get("settlements", []) or [])
        except Exception as exc:  # noqa: BLE001
            log.warning("reconciler settlements failed: %s", exc)
        return settlements

    async def reconcile(
        self,
        ticker: str | None = None,
        shadow_metrics: dict[str, Any] | None = None,
        ai_live_metrics: dict[str, Any] | None = None,
        human_metrics: dict[str, Any] | None = None,
    ) -> Scoreboard:
        fills = await self.fetch_fills(ticker)
        settlements = await self.fetch_settlements(ticker)
        sb = build_scoreboard(fills, settlements, shadow_metrics)
        # Override AI_LIVE with the bot's own ledger (authoritative, never ages out).
        if ai_live_metrics:
            sb.ai_live.wins = int(ai_live_metrics.get("wins", 0) or 0)
            sb.ai_live.losses = int(ai_live_metrics.get("losses", 0) or 0)
            sb.ai_live.pnl = float(ai_live_metrics.get("pnl", 0.0) or 0.0)
        # Override HUMAN_MANUAL with the operator's manually-entered trades (since the
        # live/historical API can't recover them).
        if human_metrics:
            sb.human_manual.wins = int(human_metrics.get("wins", 0) or 0)
            sb.human_manual.losses = int(human_metrics.get("losses", 0) or 0)
            sb.human_manual.pnl = float(human_metrics.get("pnl", 0.0) or 0.0)
        return sb
