-- Idempotent. Safe to re-run.
CREATE TABLE IF NOT EXISTS strategy_versions (
  version_id TEXT PRIMARY KEY,
  market_key TEXT NOT NULL DEFAULT 'btc_15m',
  parent_version_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('CHAMPION','CHALLENGER','REJECTED','ARCHIVED')),
  config_json JSONB NOT NULL,
  metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  gate_checks_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_by TEXT DEFAULT 'learning_loop',
  evaluated_at TIMESTAMPTZ DEFAULT NOW(),
  promoted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_active_champ
  ON strategy_versions (market_key, status) WHERE status = 'CHAMPION';

CREATE TABLE IF NOT EXISTS btc_15m_window_evaluations (
  window_start_sec BIGINT PRIMARY KEY,
  strike_price NUMERIC(12,2) NOT NULL,
  close_price NUMERIC(12,2) NOT NULL,
  outcome SMALLINT NOT NULL CHECK (outcome IN (0,1)),
  regime TEXT NOT NULL CHECK (regime IN ('chop','trend','spike')),
  features_json JSONB NOT NULL,
  market_ask_up NUMERIC(5,4) NOT NULL,
  market_ask_down NUMERIC(5,4) NOT NULL,
  fee_rate NUMERIC(5,4) NOT NULL DEFAULT 0.0150,
  champion_version_id TEXT,
  champion_prob_up NUMERIC(5,4),
  created_at TIMESTAMPTZ DEFAULT NOW()
);
