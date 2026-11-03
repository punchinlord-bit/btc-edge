# Paste this into Cline (files are already uploaded in the workspace)

## ROLE
You are a senior Node.js engineer. Integrate the pre-written files below into our existing BTC 15-minute Kalshi + Polymarket prediction engine (Express 4, Supabase/PostgreSQL, WebSocket, vanilla HTML/CSS/JS).

## HARD RULES
- Do NOT rewrite or break existing news scrapers, grading loops, broker/execution modules, or the current dashboard.
- Do NOT regenerate the uploaded files from scratch. Place them, then make minimal wiring edits.
- Never put API keys or wallet secrets in the frontend or in any API response. Paper mode stays the default.
- Run `node --test` at the end. Report results and any failures honestly.

## FILES TO PLACE (paths relative to repo root)
- `sql/migrations/strategy_registry_and_gates.sql`
- `src/engine/promotionGate.js`
- `src/routes/strategyGateRoutes.js`
- `test/promotionGate.test.js`
- `public/rocketChart.html`

## STEPS
1. **Discover.** Read `package.json`, the migration folder convention, the server entry, the WebSocket broadcaster, and where the 15m model computes pUp, strike, price and regime. If `package.json` has `"type": "module"`, convert the CommonJS files to ESM (or rename to `.cjs`) without changing logic.
2. **Migration.** Apply the SQL using our existing migration method. Seed one row in `strategy_versions` with `status='CHAMPION'`, `market_key='btc_15m'`, `config_json` = our current live model parameters.
3. **Mount routes.** In the server entry: `app.use('/api/btc-15m', createStrategyGateRoutes({ supabase, broadcast, getLiveState, getOosWindows, predict }))`.
   - `broadcast(msg)`: send JSON to all WS clients.
   - `getLiveState()`: return `{ strikePrice, currentPrice, championVersionId, championProbUp, challengerProbUp, orderbookImbalance, latestNewsSignal }` from the real model.
   - `getOosWindows()`: read the last N rows of `btc_15m_window_evaluations` and map to `{ outcome, regime, features, askUp, askDown, feeRate }`.
   - `predict(config, features)`: call our real model with that config and return pUp. If omitted, the default logistic over `config.weights`/`config.bias` is used.
4. **Ledger writes.** At each 900s window rollover, insert the finished window into `btc_15m_window_evaluations` (outcome, regime, features, asks, champion prob). Classify regime with our existing realized-vol logic into `chop | trend | spike`.
5. **Learning loop.** Change it so it NEVER edits live CHAMPION weights. Every proposed tweak is inserted as `status='CHALLENGER'`. Promotion only happens through `POST /api/btc-15m/gate/evaluate`.
6. **Live execution.** The execution layer must read the CHAMPION config from `strategy_versions` and refuse to trade on any other version. Operator keys come from env: `OP1_KALSHI_KEY_ID`, `OP1_POLY_WALLET`, `OP2_KALSHI_KEY_ID`, `OP2_POLY_WALLET`. Respect each operator's armed flag from `/operators/:id/arm`.
7. **Frontend.** Serve `public/rocketChart.html` (e.g. `/rocket`). It auto-detects the API on the same origin. For live ticks pass `?ws=wss://HOST/PATH`; the WS must send `{"type":"tick","price":number}` and may send `{"type":"gate","result":...}`. Add a tick emitter to our existing WS broadcaster if one does not exist. Do not edit the chart's visuals.
8. **Tests.** Run `node --test`. All 5 promotion gate tests must pass, plus all pre-existing tests.

## ACCEPTANCE CHECKLIST
- [ ] `GET /api/btc-15m/telemetry` returns window times, strike, price, champion id, pUp, gate scorecard, operators (booleans only).
- [ ] `POST /api/btc-15m/gate/evaluate` rejects with a clear error when there is no champion, no challenger, or < 150 OOS windows.
- [ ] A passing challenger becomes CHAMPION and the old one ARCHIVED in one upsert.
- [ ] `/rocket` shows live price, strike line, countdown, model cone, gate card, operator strip.
- [ ] No secrets in any response or in the browser.
