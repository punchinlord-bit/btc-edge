CREATE TABLE IF NOT EXISTS btc_15m_bets (
  id TEXT NOT NULL,
  venue TEXT NOT NULL CHECK (venue IN ('kalshi','polymarket')),
  side TEXT NOT NULL CHECK (side IN ('UP','DOWN')),
  ts TIMESTAMPTZ NOT NULL,
  qty NUMERIC NOT NULL DEFAULT 0,
  cents NUMERIC,
  bot TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  window_start_sec BIGINT NOT NULL,
  PRIMARY KEY (venue, id)
);
CREATE INDEX IF NOT EXISTS idx_btc_15m_bets_window ON btc_15m_bets (window_start_sec, venue);
