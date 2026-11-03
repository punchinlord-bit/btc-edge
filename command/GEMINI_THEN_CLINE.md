# Rocket chart fix (v3): brief for Gemini, then Cline

## What the video (rockets.mp4) shows
A dashboard with a KALSHI 15M panel and a POLYMARKET 15M panel side by side. Each panel has a header strip (build, ARMED, TEST BET, Bal, W/L, Net), a stats row (15M STRIKE, LIVE, AI FAIR / EDGE, 90S / 5M / 15M toggle, countdown ring), and the rocket canvas. Keep all of that. Only the canvas is being replaced.

Problems seen in the canvas:
1. **Vertical neon spikes** dropping from the line to the bottom edge (one or two per chart). These are lone bad ticks (about $100 off) being drawn as real prices.
2. **Hard green slab** under the line. The fill is anchored to the target, which sits far below the visible range, so it paints a solid block instead of a soft area.
3. **Flat, stair-stepped line pinned at the top.** The y-range was stretched to include a target about $180 away, so the actual price action is squashed into a thin band.
4. **Left two-thirds of the chart is empty.** History starts at page load; nothing was backfilled.
5. **Same strike on both panels ($76,637.60) while the two live prices differ by about $125-140** (Kalshi ~$76,819 vs Polymarket ~$76,958). Two venues on different feeds cannot share one strike. At least one strike is not coming from that venue's own feed. This is a data bug outside the canvas.

## What v3 changes (already built and tested: `public/js/rocketChart.js`)
- Lone-spike filter: a tick more than 0.05% off the recent median is held and only kept if the next tick confirms it. Spikes vanish; real moves pass one tick later.
- Soft gradient area under the line that fades out; line colored green/red by side of target.
- Smooth curve instead of stair-steps.
- Y-range follows the PRICE. Target joins the range only when it is near the action; otherwise a pill at the edge shows `▼ TARGET $76,637.60 (−$181)`.
- Per-chart views: 90s and 5m are live streams (rocket pinned at 78% riding the line), 15m is the full round. Same labels as your toggle.
- Calmer flame particles. Bets are small markers plus one average-entry line per side (no connector lines to the rocket).
- `?debug=1` prints per chart: raw price, ticks, rejected ticks, seconds since last tick, timestamp skew, strike and EST flag, and a red warning when both venues show the same strike with different prices.

## Instructions for Cline
Do not rebuild the dashboard. Do not touch the header strips, ARMED/bot logic, scrapers, or broker code.
1. Place `public/js/rocketChart.js`. Before loading it set `window.ROCKET_NO_DEMO = true`.
2. Delete the current canvas drawing code and any second tick pipeline feeding it. In each panel's existing chart container (must have a fixed height) call:
   `const kalshi = RocketChart.mount(kalshiEl, 'kalshi', { view: 90 })` and `const poly = RocketChart.mount(polyEl, 'polymarket', { view: 90 })`.
3. Wire the existing 90S / 5M / 15M buttons to `chart.setView(90 | 300 | 900)`. Tap-to-cycle is built in and can be disabled with `{ tapToCycle: false }`.
4. Feeds (each venue gets its OWN stream, source timestamp included):
   - Polymarket: Chainlink BTC/USD ticks, `rocket.tick('polymarket', price, sourceTs)`.
   - Kalshi: CF BRTI ticks, `rocket.tick('kalshi', price, sourceTs)`.
   - Never use `window.tick(price)` or a tick without `venue`; that feeds both charts the same price.
   - Bets: `rocket.bet(venue, { id, side:'UP'|'DOWN', ts, qty, cents, bot })` where `cents` is price paid per contract (0-100), never a BTC price.
5. **Backfill on load** so the chart is full immediately: `rocket.seed(venue, [{t, p}, ...])` with at least the last 15 minutes of that venue's ticks (oldest first). Keep a rolling buffer server-side or in a Map per venue so a refresh does not empty the chart.
6. **Fix the strike per venue** (item 5 above): `rocket.setStrike('polymarket', windowStartSec, chainlinkPriceAtOpen)` and `rocket.setStrike('kalshi', windowStartSec, avgBrti60sBeforeOpen)`. The dashboard's STRIKE / LIVE / AI FAIR numbers should read from the same values (`RocketChart.charts.kalshi.strike`, `.dispM`) so the panel text and the graph can never disagree.
7. Open `/your-dashboard?debug=1` and report the debug lines for both charts, plus any red warning.

## Acceptance
- [ ] No vertical spikes. No slab. Line is smooth and fills the chart height.
- [ ] Chart is full on page load (backfill) and after refresh.
- [ ] Different lines AND different strikes per venue; strikes match each venue's own rule.
- [ ] Rocket rides the line in the 90s and 5m views; 15m shows the whole round.
- [ ] Panel numbers (LIVE, STRIKE) match the graph exactly.
- [ ] `rejected` stays near 0 in debug; no red warning.
