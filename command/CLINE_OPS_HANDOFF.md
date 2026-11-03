# Cline: ready-check both venues, run the shadow engine nonstop, add a live watchdog + Command Deck widget

## HARD RULES (read first)
- **Do not place, cancel, or modify any real order. Do not arm or disarm anything. Do not change risk limits. Do not move or deposit funds.** Everything below is read-only checks plus paper (shadow) trading.
- Preflight probes must be read-only. No secrets in any response, log line, or the browser. Report booleans and masked values only.
- Do not rewrite scrapers, the brain, or broker modules. Add small hooks. Run `node --test` at the end and report results honestly, including anything you could not verify.
- If part of the stack is Python (for example `btc_15m_brain.py`), do not port it. Have it report over HTTP (snippet in step 4).

## Files to place (keep paths)
`src/engine/opsMonitor.js`, `src/routes/opsRoutes.js`, `src/engine/betFeed.js` (UPDATED: now carries `mode`/`result`/`pnl` and an `onRecord` hook), `public/js/opsWidget.js`, `public/opsWidget.html` (demo page), `sql/migrations/btc_15m_ops.sql` (run AFTER `btc_15m_bets.sql`), `test/opsMonitor.test.js`, `test/opsWidget.test.js`, and the updated `public/js/rocketChart.js` (shadow bets now draw as ghost markers and never count toward live P&L).

## 1. Wire it up
```js
const ops = createOpsMonitor({ onEvent: (e) => persistOpsEvent(e) /* insert into ops_events */, onDead: (name) => restartComponent(name) });
ops.start();                                            // checks every 2s
const betFeed = createBetFeed({ broadcast, persist, onRecord: (rec) => ops.recordTrade(rec) });  // one call feeds chart + ops log
app.use('/api/btc-15m', createOpsRoutes(ops, { probes, broadcast }));
```
Set `OPS_KEY` in the environment. Remote writes (`POST /ops/heartbeat`, `/ops/trade`) need header `x-ops-key`. Without the key only localhost can write.

## 2. Mount the widget in the panel UI
One mount, full width, above the two venue panels (or in a collapsible strip): `OpsWidget.mount(el, { api: '' })`. Do not pass `demo`. If the backend is unreachable the widget shows a red "WATCHDOG IS BLIND" banner on its own; confirm that works by stopping the server.

## 3. Heartbeats at the real work points (this is what makes the checker honest)
Call `ops.beat(name, { progress })` where `progress` is a counter that only increases when REAL work happens. Beats with a frozen counter become **ZOMBIE** (alive but doing nothing).
| name | where | progress counter |
|---|---|---|
| `kalshi-feed` | BRTI tick handler (throttle to 1/s) | ticks received |
| `polymarket-feed` | Chainlink/RTDS tick handler | ticks received |
| `kalshi-exec`, `polymarket-exec` | every execution-loop cycle, even when there is no signal | cycles run |
| `shadow-engine` | every shadow cycle | shadow trades placed + graded |
| `brain` | every learning-loop pass | challengers proposed/evaluated |
| `grader` | every settlement pass | rounds graded |
Thresholds live in `DEFAULTS` in `opsMonitor.js` (shadow zombie after 20 min of no progress; feeds after 30 s). Allow overriding via env (for example `OPS_IDLE_SHADOW_SEC`) so you can test quickly.

## 4. Stop things dying silently
- Wrap every long-running loop: `supervise('shadow-engine', async (ctx) => { while (!ctx.isStopped()) { await cycle(); ctx.beat({ progress: n }); await sleep(...) } }, { monitor: ops })`. A throw restarts it with backoff and counts a crash (the tile shows 💥 count).
- Add `process.on('unhandledRejection')` and `process.on('uncaughtException')` handlers that call `ops.crash(...)` and log the stack, instead of letting the process or a loop vanish.
- Find why it "dies randomly": check the hosting logs for restarts, out-of-memory kills, instances sleeping on idle (if on a free tier, that alone stops trading and needs an always-on instance or an external pinger on `GET /api/btc-15m/ops`), unhandled promise rejections inside websocket reconnect code, and feeds that stop reconnecting after a close. Report the root cause you find, not just the symptom.
- Python pieces: `requests.post(BASE + "/api/btc-15m/ops/heartbeat", json={"name": "brain", "progress": passes}, headers={"x-ops-key": KEY}, timeout=3)` once per loop pass (swallow errors; never let the heartbeat crash the loop).

## 5. Shadow engine: run nonstop on BOTH venues and grade every round
It has not been running. Make it run, in the same supervised process:
1. Each 15m round, after the model has a pUp, place a paper trade per venue with the Champion (optionally one per challenger, with `bot` = version id). Entry price is the real best ask for that side at decision time (so fees and spread are honest). If no price is available, skip and log why. Record with `betFeed.recordBet(venue, { id: 'shadow-<venue>-<windowStartSec>-<n>', mode: 'shadow', side, qty, cents, bot, ts })`.
2. **HARD GUARD:** shadow mode must never reach any exchange order endpoint. Put the guard at the lowest layer that sends orders (`if (mode === 'shadow') throw`). Add a unit test that proves a shadow trade cannot call the order function.
3. At each venue's settlement (its own rule: Polymarket = Chainlink close vs open; Kalshi = 60s BRTI average at close vs the 60s average before open), call `gradeRound(trades, up)` then `ops.gradeTrade(id, grade)` and `betFeed.recordBet` with `result`/`pnl`. Insert the round into `btc_15m_window_evaluations` (outcome, regime, features, asks, champion prob).
4. Feed graded shadow results to the learning loop as CHALLENGER proposals. They only become CHAMPION through the promotion gate. The widget's XP bar is the graded count toward the gate's 150-window floor.

## 6. Ready-check both venues (read-only probes)
Implement `probes.kalshi` and `probes.polymarket`, each `async () => ({ ok, warn?, detail })`:
- `creds`: keys present and well-formed; authenticated read works. Polymarket: API credentials valid, wallet funded, allowances set.
- `api`: one authenticated read endpoint answers in under 2 s.
- `market`: the currently OPEN 15m BTC market exists, is accepting orders, closes at the next :00/:15/:30/:45, and the ticker/token ids match what the executor will use. A wrong market lookup is a classic reason nothing trades.
- `balance`: enough for one position plus fees, and at least the exchange minimum order size.
- `strike`: this round's target is set and comes from THIS venue's own feed (they must differ between venues).
- `clock`, `risk` (kill switch state, max size configured), `armed` (warn only; do not change it).
Required to be READY: creds, api, market, balance, strike. An unwired required probe counts as NOT READY on purpose.

**Then investigate and report why neither venue has traded in a long time.** Check last order timestamps in the DB or logs, and which gate blocked each signal (edge threshold, risk cap, stale feed, expired credentials, wrong market, balance). From the dashboard recording, the displayed balances were about $13.76 on Kalshi and $1.81 on Polymarket. Check whether those are below the minimum order size or fee floor, and tell me what you find. Do not deposit or change anything.

## Acceptance (report each honestly)
- [ ] Widget shows 7 tiles, two venue cards with checklists, XP bar, and a trade log with live and shadow rows (👻 vs ⚔️), times, results.
- [ ] Kill the shadow engine: tile goes LAGGING then OFFLINE within about 90 s, banner flashes, `ops_events` row written, supervisor restarts it.
- [ ] Freeze its progress counter while still beating: ZOMBIE appears (use the env override), then clears when progress resumes.
- [ ] Stop the server: widget shows WATCHDOG IS BLIND.
- [ ] Each venue card is READY only when all required probes pass; a failing probe shows ✖ with its reason on hover.
- [ ] Shadow trades appear on the correct venue chart as ghost markers, never in live tally; graded results arrive at each round's settlement.
- [ ] `node --test` passes (including the shadow no-order guard test). List anything not verified.
