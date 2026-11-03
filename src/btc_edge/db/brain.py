"""SQLite "brain" store: WAL-mode persistence for outcomes, trades, and rollups.

A dedicated database (``data/btc_15m_brain.db``) decoupled from the main app DB,
configured with WAL + synchronous=NORMAL so writes are fast and reads never block.
The dashboard/telemetry read from in-memory state + ``venue_rollups`` and never
wait on a slow exchange API pagination.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btc_edge.db.models import Base, BotTrade, VenueRollup, WindowOutcome

logger = __import__("logging").getLogger(__name__)


class BrainStore:
    """Persists the 15m brain ledger (outcomes, trades, venue rollups)."""

    def __init__(self, path: str | Path = "data/btc_15m_brain.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path.resolve()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        with self.engine.begin() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA synchronous=NORMAL"))
        Base.metadata.create_all(self.engine)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self):
        return self._factory()

    def record_trade(self, trade: dict[str, Any]) -> None:
        """Upsert a bot order by ``trade_id`` (same id = update status/pnl)."""
        try:
            with self.session() as s:
                row = s.get(BotTrade, trade["trade_id"])
                if row is None:
                    row = BotTrade(trade_id=trade["trade_id"])
                    s.add(row)
                row.venue = trade.get("venue", "")
                row.window_start_ts = int(trade.get("window_start_ts", 0))
                row.placed_at = float(trade.get("placed_at", time.time()))
                row.dir = trade.get("dir", "up")
                row.entry_cents = int(trade.get("entry_cents", 0))
                row.contracts = float(trade.get("contracts", 0))
                row.btc_price_at_entry = trade.get("btc_price_at_entry")
                row.status = trade.get("status", "open")
                row.settled_pnl_usd = trade.get("settled_pnl_usd")
                row.won = trade.get("won")
                s.commit()
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("brain record_trade failed: %s", exc)

    def record_outcome(self, outcome: dict[str, Any]) -> None:
        """Upsert a resolved window by ``window_start_ts``."""
        try:
            with self.session() as s:
                row = s.get(WindowOutcome, int(outcome["window_start_ts"]))
                if row is None:
                    row = WindowOutcome(window_start_ts=int(outcome["window_start_ts"]))
                    s.add(row)
                for k in (
                    "kalshi_strike", "kalshi_close", "kalshi_outcome",
                    "poly_strike", "poly_close", "poly_outcome",
                    "model_prob_up", "brier_score", "regime",
                ):
                    if k in outcome:
                        setattr(row, k, outcome[k])
                s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain record_outcome failed: %s", exc)

    def upsert_rollup(self, venue: str, rollup: dict[str, Any]) -> None:
        """Update the cached per-venue balance / win-loss / P&L row."""
        try:
            with self.session() as s:
                row = s.get(VenueRollup, venue)
                if row is None:
                    row = VenueRollup(venue=venue)
                    s.add(row)
                row.balance_usd = rollup.get("balance_usd")
                wins = int(rollup.get("wins", 0))
                losses = int(rollup.get("losses", 0))
                tot = wins + losses
                row.wins = wins
                row.losses = losses
                row.win_rate = (wins / tot) if tot else 0.0
                row.total_pnl_usd = float(rollup.get("total_pnl_usd", 0.0))
                row.last_bet_json = json.dumps(rollup.get("last_bet") or {})
                row.updated_at = time.time()
                s.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain upsert_rollup failed: %s", exc)

    def get_rollup(self, venue: str) -> dict[str, Any]:
        """Read the persisted scorecard for a venue (empty if never written)."""
        try:
            with self.session() as s:
                row = s.get(VenueRollup, venue)
                if row is None:
                    return {}
                return {
                    "balance_usd": row.balance_usd,
                    "wins": row.wins,
                    "losses": row.losses,
                    "win_rate": row.win_rate,
                    "total_pnl_usd": row.total_pnl_usd,
                    "last_bet": json.loads(row.last_bet_json or "{}"),
                    "updated_at": row.updated_at,
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("brain get_rollup failed: %s", exc)
            return {}

    def close(self) -> None:
        self.engine.dispose()
