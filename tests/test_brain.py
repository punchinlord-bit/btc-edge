"""Tests for the SQLite "brain" store (WAL-mode outcomes/trades/rollups)."""

from __future__ import annotations


def test_brain_store_roundtrip(tmp_path):
    from btc_edge.db.brain import BrainStore
    from btc_edge.db.models import BotTrade, VenueRollup, WindowOutcome

    brain = BrainStore(tmp_path / "brain.db")
    try:
        brain.record_trade({
            "trade_id": "kalshi-up-buy-1",
            "venue": "kalshi",
            "window_start_ts": 1_762_032_000,
            "placed_at": 1_762_032_100.0,
            "dir": "up",
            "entry_cents": 58,
            "contracts": 25,
            "btc_price_at_entry": 77_444.2,
            "status": "filled",
        })
        brain.record_outcome({
            "window_start_ts": 1_762_032_000,
            "regime": "trend",
            "kalshi_strike": 77_440.0,
            "kalshi_close": 77_452.8,
            "kalshi_outcome": "up",
        })
        brain.upsert_rollup("kalshi", {
            "balance_usd": 100.0, "wins": 3, "losses": 1, "total_pnl_usd": 12.5,
        })

        with brain.session() as s:
            t = s.get(BotTrade, "kalshi-up-buy-1")
            assert t is not None and t.venue == "kalshi" and t.entry_cents == 58
            o = s.get(WindowOutcome, 1_762_032_000)
            assert o is not None and o.kalshi_outcome == "up"
            r = s.get(VenueRollup, "kalshi")
            assert r is not None and r.wins == 3 and r.losses == 1
            assert r.win_rate == 0.75
    finally:
        brain.close()


def test_brain_upsert_updates_same_trade(tmp_path):
    from btc_edge.db.brain import BrainStore
    from btc_edge.db.models import BotTrade

    brain = BrainStore(tmp_path / "brain2.db")
    try:
        brain.record_trade({
            "trade_id": "t2", "venue": "kalshi", "window_start_ts": 1_762_032_000,
            "placed_at": 1_762_032_100.0, "dir": "up", "entry_cents": 50,
            "contracts": 10, "status": "open",
        })
        brain.record_trade({
            "trade_id": "t2", "venue": "kalshi", "window_start_ts": 1_762_032_000,
            "placed_at": 1_762_032_100.0, "dir": "up", "entry_cents": 50,
            "contracts": 10, "status": "settled", "settled_pnl_usd": 4.5, "won": 1,
        })
        with brain.session() as s:
            t = s.get(BotTrade, "t2")
            assert t.status == "settled"
            assert t.settled_pnl_usd == 4.5
            assert t.won == 1
    finally:
        brain.close()
