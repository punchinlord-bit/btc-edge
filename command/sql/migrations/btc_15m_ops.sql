-- Run AFTER btc_15m_bets.sql. Idempotent.
ALTER TABLE btc_15m_bets ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'live';   -- 'live' | 'shadow'
ALTER TABLE btc_15m_bets ADD COLUMN IF NOT EXISTS result TEXT;                          -- 'WIN' | 'LOSS'
ALTER TABLE btc_15m_bets ADD COLUMN IF NOT EXISTS pnl NUMERIC;
CREATE TABLE IF NOT EXISTS ops_events (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  type TEXT NOT NULL,        -- crash | dead | zombie | recovered
  component TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_events_ts ON ops_events (ts DESC);
