# BTC-EDGE Build Progress

Autonomous 7-phase execution tracker. Updated after each phase passes `pytest` + `ruff`/`mypy`.

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | Project scaffolding, config & database layer | [x] COMPLETE |
| Phase 2 | Exchange connectors & 500-window bootstrapper | [x] COMPLETE |
| Phase 3 | Regime filter, feature engine & deep pattern builder | [x] COMPLETE |
| Phase 4 | 4 core strategies & fee-aware backtester | [x] COMPLETE |
| Phase 5 | Self-learning ensemble, execution router & safety watchdog | [x] COMPLETE |
| Phase 6 | FastAPI web dashboard & PySide6 desktop app | [x] COMPLETE |
| Phase 7 | End-to-end verification & first-run validation report | [x] COMPLETE |
| Phase 8 | News intelligence & self-healing filter module | [x] COMPLETE |
| Phase 9 | Always-on deployment, simple UX & safety hardening | [x] COMPLETE |
| | Phase 10 | Dashboard auth (A1) + professional desktop console | [x] COMPLETE |

## Verification Log

### Phase 1 — Scaffolding, Config & DB
Created directory tree, `pyproject.toml`, `config/config.yaml`, `.env.example`,
`Dockerfile`, `docker-compose.yml`, and SQLAlchemy 2.0 SQLite models (windows /
signals / orders / fills / settlements / rule_metrics). `pytest` 55 passed, `ruff`
clean, `mypy` clean.

### Phase 2 — Connectors & Bootstrapper
Implemented Coinbase WS + 60s BRTI rolling calculator, Bybit funding/OI/liquidation
feed, Deribit DVOL/ATM IV fetcher, Kalshi v2 REST/WS client (Ed25519 + RSA-PSS
signing with query-stripped path, `yes_bid_size_fp`/`yes_ask_size_fp`, 1 req/sec
token bucket), Polymarket `polymarket-client` wrapper + Gamma slug resolver + CLOB WS,
and the deterministic synthetic bootstrapper (jump-diffusion + realistic books).

### Phase 3 — Regime, Features & Pattern Builder
Implemented `regime.py` (DAMPENING/AMPLIFYING), `indicators.py` (Window Delta,
`N(d2)` digital probability, book imbalance, micro-acceleration), and
`pattern_engine.py` (serializable Condition/RuleGroup AND/OR trees).

### Phase 4 — Strategies & Backtester
Implemented `fee_models.py` (exact Polymarket/Kalshi fees, 5-share min, slippage),
all 4 strategies, `simulator.py`, and `report.py`.

### Phase 5 — Ensemble, Router & Safety
Implemented `ml/model.py` (online logistic regression + LightGBM ensemble),
`ml/nightly_grader.py` (trailing-200 EV/Brier/Sharpe + auto-quarantine),
`execution/router.py` (Quarter-Kelly + paper/live dual gate), and
`execution/safety.py` (30s disconnect flush + circuit breaker).

### Phase 6 — Web & Desktop
Implemented `web/app.py` (FastAPI + WebSocket + Kill Switch),
`web/templates/index.html` (dark dashboard), `desktop/gui.py` (PySide6, lazy import),
and `desktop/gui_fallback.py` (>$50 confirmation gate).

### Phase 7 — End-to-End Verification
- `python scripts/run_bootstrap.py --synthetic-if-offline --windows 600` ->
  600 windows written to `data/historical/windows.jsonl`.
- `python scripts/run_backtest.py` -> `BACKTEST_VALIDATION_REPORT.md` generated.
- Full suite: `pytest` (55 passed), `ruff` (clean), `mypy` (no issues in 37 files).

### Phase 8 — News Intelligence & Self-Healing Filter
Implemented the self-learning news module:
- `connectors/news_scraper.py` — async RSS/Atom scraper with Bitcoin relevance filtering.
- `features/sentiment_analyzer.py` — financial-lexicon NLP (offline) + optional
  HuggingFace transformer backend, outputting a normalized `[-1, +1]` score.
- `ml/self_healing_filter.py` — the "brain": tracks per-source predictive accuracy,
  auto-downgrades/disables noisy sources, and self-heals them back once accuracy
  recovers. Produces a reliability-weighted aggregate sentiment.
- Integrated `news_sentiment` into the ensemble feature vector, `MarketContext`,
  config (`news:` section), and DB (`news_articles` / `news_source_metrics` tables).
- `scripts/run_news.py` — standalone pipeline runner.
- Full suite: `pytest` (70 passed), `ruff` (clean), `mypy` (no issues in 40 files).

### Phase 9 — Always-on Deployment, Simple UX & Safety Hardening
- **Safety**: persisted out-of-band kill switch (`execution/killswitch.py`),
  append-only audit log (`execution/audit.py`), idempotency keys on orders,
  trade-only-key enforcement flag, Telegram alerting (`execution/alerts.py`).
- **AI assistant**: `ml/ai_assistant.py` — read-only OpenAI-compatible explainer
  (works with ChatGPT *and* DeepSeek), with a deterministic offline fallback.
- **Simple UX**: upgraded dashboard with big RESUME/HALT buttons, a plain-English
  "what the bot thinks" panel, "at risk" readout, news tone, signals, and the
  read-only AI chat panel. Desktop popup (PySide6) retained.
- **Always-on**: `render.yaml` blueprint + `/api/health` + Dockerfile health,
  `PORT` env override, persistent disk, paper-first by default.
- **Real-data validation**: `scripts/import_predictions.py` forward-tests a live
  prediction log (net EV / win rate / Brier / Sharpe).
- Full suite: `pytest` (72 passed), `ruff` (clean), `mypy` (no issues in 45 files).

### Phase 10 — Dashboard Auth (A1) & Professional Desktop Console
- **A1 fixed (Critical)**: `DASHBOARD_API_KEY` shared-secret auth via `require_auth`
  dependency in `web/app.py`, applied to every `/api/*` route (except the public
  `/api/health` probe) and `/ws/live`. `render.yaml` + `.env.example` updated;
  `index.html` prompts once and caches the key in `localStorage`.
  `tests/test_auth.py` (4 tests) covers enforcement, bypass, and public health.
- **Professional desktop console**: rewrote `desktop/gui.py` as a polished dark
  PySide6 panel (live mode/regime/status badges, sentiment + at-risk + open-strike
  + countdown metrics card, strategy net-EV/win-rate table, signals feed, system
  tray, "Open Dashboard" launcher) wired to a new testable
  `desktop/automation.py` (`OperatorActions`: halt/resume/snapshot + app-level
  `Ctrl+Shift+K` / `Ctrl+Shift+R` safety hotkeys). `scripts/run_desktop.py` now
  binds the persisted kill switch + configured dashboard URL.
- `tests/test_desktop.py` (5 tests) — halt/resume persistence, snapshot, hotkeys,
  and the >$50 confirmation gate.
- **Mouse automation**: `desktop/mouse_automation.py` (`MouseAutomation`,
  `DashboardMouseActions`, `MouseAction`, `locate_center`) — safe, testable
  PyAutoGUI primitives (move/click/double-click/scroll) with an optional
  pixel "safe zone" that blocks out-of-bounds actions, plus
  `scripts/run_mouse.py` CLI. `tests/test_mouse_automation.py` (8 tests) uses an
  injected fake backend so it runs without moving the real cursor.
- Full suite: `pytest` (95 passed), `ruff` (clean). PySide6 6.12 and pyautogui
  0.9.54 installed into the venv so `python scripts/run_desktop.py` and
  `python scripts/run_mouse.py` run natively.

### Swarm Brain (20-agent consensus) — Quorum Execution Gate

Added a 20-agent "swarm Brain" (`src/btc_edge/brain/swarm.py`) with 4 archetypes
(5 Tape Snipers, 5 Strike Pinners, 5 Mean Reverters, 5 Book Watchers), vectorized
dot-product inference, Brier-weighted trimmed consensus, dynamic shrinkage, and
direction-split quorum (`quorum_up`/`quorum_down`). Wired it into the trading loop
as the execution gate:

- **Entry gate** (`_filter_entries`): a BUY only fires when the swarm's quorum on
  that side is `>= 14/20` AND the dispersion IQR is `<= 0.08` (`SWARM_MIN_QUORUM`,
  `SWARM_MAX_DISPERSION`). This replaces "any strategy with a hair of edge fires"
  with a robust quorum vote, stopping over-trading.
- **Sensitive scratch exit** (`_manage_positions`): while holding, a drop below
  `SWARM_EXIT_QUORUM` (7/20) on the position's side triggers an early exit before
  market makers re-price against us.
- Swarm consensus exposed per-venue via `/api/btc-15m/telemetry` (`swarm` field).

`tests/test_swarm.py` (5 tests) covers latency, quorum outlier rejection, dynamic
shrinkage, calm-market shrink, and update tracking. `test_swarm.py` +
`test_strategies_and_backtest.py` pass; `ruff` clean.

Remaining swarm work: **Phase 1** — a true sub-second (100 ms) tape feed so the
Tape Sniper archetype's micro-velocity features stop defaulting to 0.

### Brain completion + mobile chart fix (finalization)

- **Tape features now live**: `TradingLoop._tape_feats()` derives short-horizon
  `micro_velocity_500ms` / `micro_velocity_2s` (percent-per-second) and
  `realized_vol_10s` (annualized) from the existing 1s price buffer, so the Tape
  Sniper archetype receives real signal instead of flat 0. The brain is now a
  complete learn → predict → execute loop: `ens.update`/`retrain_lgbm` +
  `swarm.update` on every settlement (learn), `ens.predict` + `swarm.consensus`
  (predict), router + quorum gate (execute).
- **Mobile rocket chart fixed**: `window.rocket` hooks (`seed`/`tick`/`setStrike`/
  `setModel`/`bet`/`syncClock`) were never merged onto `window.RocketChart`, so the
  page's inline script (`RocketChart.seed(...)` etc.) threw on every call and the
  chart mounted blank/stale. Added `Object.assign(window.RocketChart, window.rocket)`
  and cache-busting (`?v=4`) on the static script.
- **Caches cleared** (`__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`)
  and the single engine restarted (single-instance lock on port 54321).
- Polymarket BBO feed now returns `200 OK` (the 404 synthetic-50/50 fallback is
  gone), so the model reads the real Polymarket book.



### Kalshi Reconciler + 3-Bucket Attribution + Hot Path (finalization)

- **Historical endpoints** (kalshi_client.py): get_historical_cutoff / get_historical_orders / get_historical_fills.
- **Reconciler** (execution/reconciler.py): classify_fill (AI_v3_ prefix), build_scoreboard (AI_LIVE / AI_SHADOW / HUMAN_MANUAL), format_telegram, KalshiReconciler.
- **Never-aging ledger fallback**: ai_live_from_ledger (trades.jsonl) and human_manual_from_file (data/manual_trades.jsonl) so the AI + human scoreboards survive Kalshi's historical cutoff.
- **Hedge retrain**: lr 0.20 + [0.05, 0.55] weight clamp.
- **Hot path**: test_hot_path.py proves p99 tick latency < 1ms.
- Full suite: pytest 200 passed; ruff clean.

### Sell-to-close settlement fix (wrong "Lost $" Telegram report)

- A sell-to-close from `position_manager` / `take_profit` was being re-recorded as a
  new open `action="sell"` position, so `_settle_positions` later settled it like a
  fresh short against the window outcome. A correct "down" win (bought 0.415, closed
  0.555) was reported to Telegram as "Lost $1.26" because DOWN actually won.
- **Fix**: `StrategyDecision` now carries `entry_price` / `entry_fee` for close
  decisions; `_route_decisions` realizes the close P&L immediately via
  `TradingLoop._realize_close` (exit - entry, no window outcome) and never re-adds
  the close to `_open`.
- Regression tests: `test_realize_close_records_winning_exit` +
  `test_sell_decision_carries_entry_price`. `test_trading_loop.py` + reconciler +
  portfolio-poller + hot-path suites pass; `ruff` clean.


### Polymarket frugal-bidding: tighter price ceiling (60c)

- Polymarket was still able to throw 70-90c at its first bid of a window (the shared
  0.72 price cap only stopped 72c+). Added a venue-specific hard ceiling
  `polymarket_max_contract_price` (default **0.60**) so the PM leg only enters when
  the contract is genuinely cheap — value, not chasing — while Kalshi keeps 0.72.
- **Fix**: `RiskController` takes `polymarket_max_contract_price`; `evaluate_signal`
  picks the ceiling by venue (`0.60` for polymarket, `0.72` for kalshi). Wired from
  `config.trading.polymarket_max_contract_price` (new `TradingConfig` field +
  `_fraction` validator) into `TradingLoop`.
- Regression tests: `test_polymarket_tighter_price_ceiling_blocks_70c`,
  `test_polymarket_buys_cheap_under_60c`, `test_kalshi_keeps_72c_ceiling`.
  `test_risk_and_shadow.py` + trading-loop + reconciler + hot-path pass; `ruff` clean.


- Also tightened `polymarket_exposure_pct` 100.0 -> **20.0** in `config/config.yaml` (the
  intended default that keeps the PM leg from drunk-betting the whole bankroll).
- **Engine restarted**: stopped the two stale `run_engine.py` processes and relaunched
  via `.venv\Scripts\python.exe scripts/run_engine.py` (logs -> `server_out.txt` /
  `server_err.txt`). Verified `Live trading allowed: True`, dashboard on :8080, all
  connectors (Kalshi/Polymarket/Coinbase/Telegram/Ollama/DeepSeek) 200 OK.


### "yeaaa" celebration sound on profitable exits

- On every profitable exit (`_maybe_alert_settlement` with `pnl > 0`, i.e. a winning
  sell-to-close or settlement), the bot now bumps `EngineState.sell_win_seq` and plays
  `yeaaa_yeaaa.mp3`.
- **Telegram**: `TelegramAlerter.send_audio` uploads the MP3 via `sendAudio` alongside
  the "✅ Won …" message.
- **App**: the MP3 is served at `/static/yeaaa_yeaaa.mp3`; both `index.html` (dashboard)
  and `rocketChart.html` watch `sell_win_seq` (from `/api/metrics` + `/api/btc-15m/telemetry`
  `sellWinSeq`) and play the sound when it increments.
- `WIN_AUDIO_PATH` constant in `trading_loop.py` points at the repo-root MP3.
- Test: `test_realize_close_records_winning_exit` asserts `sell_win_seq` bumps on a win.


### Command Deck (ops watchdog + readiness + shadow engine + widget)

- Ported the Node.js `command/` reference bundle into the Python engine (no Node runtime here):
  - `execution/ops_monitor.py` — `OpsMonitor` (7 component tiles with ALIVE/STALE/DEAD/
    ZOMBIE/ERROR, progress-frozen ZOMBIE detection, trade log with live 👻/shadow ⚔️,
    grading, snapshot, `grade_round`, `run_preflight`, `supervise`).
  - `execution/ops_probes.py` — read-only per-venue probes (creds/api/market/balance/
    strike/clock/risk/armed). Secrets never leave the process.
  - `web/app.py` — `_ops` singleton + `GET /api/btc-15m/ops` (public, booleans only) +
    `POST /ops/heartbeat|trade|trade/{id}/grade` + `/ops` page.
  - Heartbeats wired: `kalshi/polymarket-exec` in `TradingLoop.tick`, `brain`/`grader` at
    settlement/learning, `kalshi/polymarket-feed` in `spot_feeds._aggregate`.
  - `run_engine.py` — nonstop paper (shadow) engine (`_shadow_engine_loop`) that places +
    grades one shadow trade per venue per 15m round, never touching an order endpoint.
  - Widget: `static/js/opsWidget.js` + `templates/opsWidget.html`, mounted full-width in
    `index.html` (`#ops-deck`) and served standalone at `/ops`.
- Tests: `tests/test_ops_monitor.py` (9 tests: ALIVE/STALE/DEAD/ZOMBIE, grading, snapshot,
  preflight fail-on-unwired). `ruff` clean.
- Removed duplicate Node.js bundles: `rocket-bundle1/`, `rocket-bundle.zip`,
  `rocket-bundle/rocket-bundle/`.
- **Readiness findings (read-only, nothing changed):** Kalshi = READY (balance ~$13.76).
  Polymarket = NOT READY — balance ~$1.81 is below the 5-share minimum (~$2.50 notional),
  and its Chainlink feed (Binance WS) is geo-blocked (HTTP 451) so `polymarket-feed` is
  UNKNOWN. Neither was deposited to or changed.


### Autonomous flow fix + health-checker bot

- **Root cause of "no fires for 2h":** both LLM veto gates were blocking every signal —
  the Ollama 7B gate returned "H" (Hold) which the code treated as a veto, and the
  DeepSeek grader vetoed the rest. The engine itself was alive and polling fine.
- **Ollama fail-open**: `_grade_buys` now only vetoes on a clear Y↔N contradiction; "H"
  and timeouts mean "no strong opinion" → the deterministic gates decide.
- **`trading.llm_gates` config flag** (default true; set `false` in `config/config.yaml`)
  — when false, both LLM gates are skipped so the fast deterministic gates (risk +4.5%
  net edge, swarm, time-decay, chase) decide alone. This is the "fastest flow" setting.
- **`scripts/health_checker.py`** — standalone autonomous watchdog: probes `/api/health`
  + `/api/btc-15m/ops` every 30s; after ~90s of unreachability it kills the stale engine
  and relaunches it, and pushes a Telegram alert. Alerts on any DEAD Command Deck tile.
- Engine + health-checker both running as separate processes; `ruff` clean; relevant
  test suites pass.


