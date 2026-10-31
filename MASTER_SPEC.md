# BTC-EDGE: Autonomous Multi-Timeframe (5m / 10m / 15m) Prediction-Market Trading Engine
## Master Engineering & Execution Specification

Autonomously build, test, and validate the complete **BTC-Edge** monorepo in Python 3.11+. Do not stop until all 7 Phases below are implemented, tested via `pytest`, and verified with an end-to-end synthetic/historical backtest run.

---

### 1. TARGET MONOREPO DIRECTORY TREE

```text
btc-edge/
├── .clinerules
├── MASTER_SPEC.md
├── PROGRESS.md
├── README.md
├── pyproject.toml
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── config/
│   └── config.yaml
├── data/
│   ├── raw/                  # Parquet files for ticks, candles, orderbooks
│   ├── historical/           # 500+ bootstrapped resolved windows
│   └── btc_edge.db           # SQLite database
├── src/
│   └── btc_edge/
│       ├── __init__.py
│       ├── config.py
│       ├── db/
│       │   ├── __init__.py
│       │   ├── models.py
│       │   └── session.py
│       ├── connectors/
│       │   ├── __init__.py
│       │   ├── coinbase_ws.py
│       │   ├── bybit_deribit.py
│       │   ├── kalshi_client.py
│       │   ├── poly_client.py
│       │   └── news_scraper.py
│       ├── historical/
│       │   ├── __init__.py
│       │   └── bootstrapper.py
│       ├── features/
│       │   ├── __init__.py
│       │   ├── regime.py
│       │   ├── indicators.py
│       │   ├── pattern_engine.py
│       │   └── sentiment_analyzer.py
│       ├── ml/
│       │   ├── __init__.py
│       │   ├── model.py
│       │   ├── nightly_grader.py
│       │   └── self_healing_filter.py
│       ├── strategies/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── late_scalp.py
│       │   ├── market_maker.py
│       │   ├── crowd_follow.py
│       │   └── iv_mispricing.py
│       ├── backtest/
│       │   ├── __init__.py
│       │   ├── fee_models.py
│       │   ├── simulator.py
│       │   └── report.py
│       ├── execution/
│       │   ├── __init__.py
│       │   ├── router.py
│       │   └── safety.py
│       ├── web/
│       │   ├── __init__.py
│       │   ├── app.py
│       │   └── templates/
│       │       └── index.html
│       └── desktop/
│           ├── __init__.py
│           ├── gui.py
│           └── gui_fallback.py
├── scripts/
│   ├── run_bootstrap.py
│   ├── run_backtest.py
│   ├── run_engine.py
│   ├── run_desktop.py
│   └── run_news.py
└── tests/
    ├── test_fees_and_signing.py
    ├── test_features_and_patterns.py
    ├── test_strategies_and_backtest.py
    └── test_safety_and_grader.py
```

### 2. DETAILED COMPONENT SPECIFICATIONS

#### A. Data Ingestion & Historical Bootstrapper
- **BRTI Proxy (coinbase_ws.py)**: sub-second rolling 60-second TWAP/index buffer from Coinbase BTC-USD. Kalshi KXBTC15M and Polymarket btc-updown-5m-* settle on a 60s average vs window-open reference.
- **Polymarket (poly_client.py)**: `polymarket-client` (`PublicClient`, `AsyncSecureClient`). Slug discovery via `window_ts = (int(time.time()) // 300) * 300` and `https://gamma-api.polymarket.com/events?slug=btc-updown-5m-{window_ts}`. CLOB WS for L2 book (<= 1s).
- **Kalshi (kalshi_client.py)**: v2 REST + WS. Sign RSA-PSS/Ed25519: payload `f"{ts_ms}{method}{path_without_query}"`. Top-of-book via `yes_bid_size_fp`/`yes_ask_size_fp`. Token bucket 1 req/sec on `/portfolio/balance`.
- **Bootstrapper (bootstrapper.py)**: fetch 500+ resolved windows + 1m candles; deterministic synthetic fallback (`--synthetic-if-offline` populates 600 windows).

#### B. Feature Layer & Deep Pattern Builder
- **regime.py**: DAMPENING vs AMPLIFYING classifier.
- **indicators.py**: `window_delta_pct` (5x-7x weight), `option_implied_prob` (digital `N(d2)`), `book_imbalance`, `micro_acceleration`.
- **pattern_engine.py**: serializable `Condition(left, operator, right)` + math offsets, combined via `RuleGroup(logical_op="AND"|"OR", conditions=[])`. Every entry logs the boolean snapshot to SQLite.

#### C. Core Strategy Set
- **late_scalp.py**: T <= 120s, prob 0.93-0.98, |window_delta| >= 0.05%, DAMPENING; net-EV aware, Post-Only maker preferred.
- **market_maker.py**: T > 120s, DAMPENING, 3 post-only levels around fair value, spread >= $0.02, 15s refresh.
- **crowd_follow.py**: 180s <= T <= 300s, market >= $0.60, Pattern Builder confluence.
- **iv_mispricing.py**: |option_implied_prob - mid| >= 4.5% divergence + orderflow confirm.

#### D. Realistic Fee-Aware Backtester
- **Polymarket**: `taker_fee = shares * 0.07 * price * (1 - price)`; maker 0.0; `shares >= 5`.
- **Kalshi**: `taker_fee = ceil(0.07 * contracts * price * (1 - price) * 100) / 100`; maker 0.0.
- **Slippage/depth**: cap fills at top-of-book size; +1 tick latency penalty on takers.

#### E. Self-Learning Loop
- Ensemble (Online Logistic Regression + LightGBM).
- **nightly_grader.py**: trailing-200-window Net EV / Win Rate / Brier / Sharpe; auto-disable (PAPER_ONLY_QUARANTINE) when net EV < 0.

#### F. Safety, Web Dashboard & Desktop App
- **safety.py**: 5s heartbeat; 30s disconnect -> `emergency_flush()` cancel-all; `max_daily_loss_pct` (5%) + quarter-Kelly cap (10%).
- **web/app.py**: FastAPI `/api/status`, `/api/metrics`, `/api/kill-switch`, `/ws/live`; dark UI with countdown, BRTI vs strike, regime badge, per-strategy EV table, equity curve, kill switch.
- **desktop/gui.py**: PySide6 mirror. **gui_fallback.py**: >$50 notional -> blocking modal confirmation.

#### G. News Intelligence & Self-Healing Filter
- **news_scraper.py**: async RSS 2.0 / Atom scraper over curated crypto/financial
  feeds (CoinDesk, CoinTelegraph, Bitcoin Magazine, Decrypt, ...), filtering
  strictly for Bitcoin-relevant items via a weighted keyword relevance score.
- **sentiment_analyzer.py**: financial NLP converting headlines to a normalized
  `[-1, +1]` sentiment score. Default backend is a domain financial lexicon
  (offline/deterministic); an optional HuggingFace transformer backend is loaded
  lazily when installed.
- **self_healing_filter.py**: the "brain". Tracks per-source predictive accuracy
  against realized price settlements, autonomously downgrading/disabling noisy
  sources and self-healing them back on recovery. Produces a reliability-weighted
  aggregate `news_sentiment` feature consumed by the ensemble estimator.

### 3. AUTONOMOUS 7-PHASE EXECUTION PLAN

- **Phase 1**: Project scaffolding, config, DB layer.
- **Phase 2**: Exchange connectors + 500-window bootstrapper (synthetic fallback).
- **Phase 3**: Regime filter, feature engine, deep pattern builder.
- **Phase 4**: 4 core strategies + fee-aware backtester.
- **Phase 5**: Self-learning ensemble, execution router, safety watchdog.
- **Phase 6**: FastAPI web dashboard + PySide6 desktop app.
- **Phase 7**: End-to-end verification (600-window bootstrap + backtest -> `BACKTEST_VALIDATION_REPORT.md`).
- **Phase 8**: News intelligence & self-healing filter module (scraper + sentiment + source reliability brain).
