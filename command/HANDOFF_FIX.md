# Handoff (Gemini -> Cline): rocket chart fix, v2

Replace `public/rocketChart.html` with the uploaded `rocket-chart-v2.html`. Do not redesign it. Keep `betFeed.js`, `betRoutes.js`, the promotion gate, and everything else as already placed.

## What was broken in v1 (confirmed in the code)
1. Every bet drew a dashed line to the rocket, so with many bets the rocket looked glued to the bets.
2. The x-axis was a fixed 15-minute window, so the line moved about 0.3px/s and looked flat and simplified. v2 defaults to a live stream view (last 150s, rocket pinned at 78% of the width, riding the line against the target). Tap the chart to cycle 60s / 150s / full 15m.
3. The line history was wiped at every rollover. v2 keeps it and shows the previous round in gray with its old target faint.
4. Timestamps: ISO strings or ms made NaN buckets and a corrupted line. v2 parses seconds, ms, numeric strings, ISO.
5. No price sanity check: any bet price/cents sent as a tick could hijack the line. v2 rejects ticks that jump more than 2% from the last price (accepts a real level shift after 6 consistent ticks).

## Bets now
Small triangles on the line (up = UP bet, down = DOWN bet), plus ONE average-entry line per side with a label, per venue. No connector lines to the rocket. Bottom-left box: contracts up/down, P&L if settled now, session W-L.

## Verify against the real feeds (this is the part only Cline can do)
Open `/rocket?debug=1`. Each chart prints: last raw price, ticks received, rejected ticks, seconds since last tick, timestamp skew flag, strike (and EST if estimated), bet count.
- `rejected` climbing = something is sending non-price numbers as ticks. Find and fix the sender (likely bet price/cents routed into `rocket.tick`).
- `tsSkew true` = source timestamps are more than 120s off the server clock, so the page fell back to arrival time. Fix the feed's timestamp field or unit.
- Both charts must receive DIFFERENT streams: Polymarket = Chainlink BTC/USD, Kalshi = CF BRTI. Never use `window.tick(price)` or a tick message without `venue`, since that feeds both charts the same price.
- `age` should stay under ~2s for Polymarket and ~2s for Kalshi (BRTI is about 1/s).
- Bets are sent as `{type:'bet', venue, id, side:'UP'|'DOWN', ts, qty, cents, bot}`. `cents` is the price paid per contract (0-100), never a BTC price.

## Acceptance
- [ ] Rocket rides the line up and down in a live stream against the dashed TARGET line on both charts.
- [ ] Different lines and different targets per venue.
- [ ] A burst of 30 bets does not clutter or tug the rocket.
- [ ] At :00/:15/:30/:45 the round flashes CLOSED UP/DOWN with P&L, old line stays visible in gray, new target appears.
- [ ] No "DEMO" tag once real ticks arrive; `rejected` stays near 0.
