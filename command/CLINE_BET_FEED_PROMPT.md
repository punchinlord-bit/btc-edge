# Cline: feed bot bets into the rocket chart, per venue, every 15-minute round

Files to place (keep paths): `public/rocketChart.html` (REPLACES the current one), `src/engine/betFeed.js`, `src/routes/betRoutes.js`, `sql/migrations/btc_15m_bets.sql` (optional persistence), `test/betFeed.test.js`.
Do not edit the chart's visuals. Do not touch scrapers, grading loops, or broker logic beyond one call per order event. Run `node --test` at the end and report honestly.

## 1. Why the two graphs looked identical
Kalshi and Polymarket must receive DIFFERENT price streams. If the page is fed with the legacy `window.tick(price)` or a WS message without a `venue`, both charts get the same price. Fix the feed:
- Polymarket chart: Chainlink BTC/USD ticks, `{"type":"tick","venue":"polymarket","price":P,"ts":SOURCE_TS}`
- Kalshi chart: CF BRTI ticks (about 1/s), `{"type":"tick","venue":"kalshi","price":P,"ts":SOURCE_TS}`
- Always send the source timestamp (`ts`, seconds or ms) from each feed, not the time we received it. That is what makes one venue visibly lead the other by a second.
- If one feed is consistently ahead or behind after that, call `window.rocket.setOffset('kalshi', seconds)` or open the page with `?kOff=-1` / `?pOff=1` (seconds, applied to that venue's tick timestamps). Measure the offset from real data; do not guess.

## 2. Wire the bets
1. In the server entry: `const betFeed = createBetFeed({ broadcast, persist })`, then `app.use('/api/btc-15m', createBetRoutes(betFeed))`. `broadcast` sends JSON to all WS clients (same one the chart uses). `persist` is optional: upsert into `btc_15m_bets` keyed by `(venue, id)`.
2. In the execution layer, call ONE line when an order is placed, filled, or cancelled:
   `betFeed.recordBet('kalshi' | 'polymarket', { id: exchangeOrderId, side: 'UP'|'DOWN', ts, qty: contracts, cents: pricePaidPerContract, bot: botName, status: 'open'|'cancelled' })`
   - Kalshi: YES = UP, NO = DOWN for the KXBTC15M "price up" market. Polymarket: Up/Down token.
   - Same `id` again = update (fill price changed or cancelled). Use the real exchange order id.
   - Record paper trades too, but tag `bot` so they are distinguishable.
3. Protect `POST /api/btc-15m/bets` with our existing auth (or remove the POST; in-process bots do not need it).
4. The page also polls `GET /api/btc-15m/bets` every 3s and merges by id, so a dropped WS message or a page refresh mid-window still shows every bet for the current round.

## 3. What the page does (already built, just verify)
- Each venue chart draws only ITS bets: a triangle at the underlying price at fill time (up = UP bet, down = DOWN bet, size = contracts, label = entry cents), plus a dashed line from each bet to where the rocket is now. Green = winning if settled now, red = losing.
- Bottom-left of each graph: contracts up/down, estimated P&L if settled now, and a session W-L and P&L tally.
- At every 15-minute boundary (server-synced clock) the round settles by that venue's own rule, flashes CLOSED UP/DOWN with bet count and P&L, clears the bets, and starts the next round with no reload.
- Rocket tilts with momentum, flame grows with speed, and the ring around it is the model's P(up).

## Acceptance
- [ ] The two charts show different lines (and different targets) with real feeds.
- [ ] A Kalshi bet appears only on the Kalshi chart, a Polymarket bet only on the Polymarket chart, within about 1s.
- [ ] Refreshing mid-window restores that round's bets.
- [ ] At :00/:15/:30/:45 bets clear and the session tally updates; P&L matches the venue's actual settlement for 5 rounds.
- [ ] No "DEMO" tag once real ticks arrive. `node --test` passes.
