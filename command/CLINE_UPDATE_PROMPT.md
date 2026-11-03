# Cline: replace the chart with the 15-minute, per-venue, graph-only version

## What was wrong
The old page defaulted to a 300s (5m) window and used ONE price feed for everything. Polymarket and Kalshi do not settle on the same number:

| | Polymarket BTC Up/Down 15m | Kalshi KXBTC15M |
|---|---|---|
| Price source | Chainlink BTC/USD data stream (not Coinbase/Binance) | CF Benchmarks BRTI |
| Target (strike) | Chainlink price at window open | 60-second average of BRTI before window open |
| Settles on | Chainlink price at window close | 60-second average of BRTI before window close |
| Resolves Up/Yes | close >= target | end average >= start average |

Windows are UTC-aligned at :00/:15/:30/:45 (900s). The new file only draws the graph (no buttons, cards or panels).

## Do this
1. Replace `public/rocketChart.html` with the uploaded file. Do not edit its visuals. Confirm there is no `winLen=300`, no 5m toggle.
2. **Feeds (the part that makes it match).** Give the page two separate tick streams over the existing WebSocket:
   - Polymarket ticks from the Chainlink BTC/USD stream. Polymarket's live-data WebSocket carries a Chainlink BTC price topic; verify the exact topic in Polymarket's docs. If we only have Coinbase/Binance prices today, say so and stop. Those will never match Polymarket exactly.
   - Kalshi ticks from CF BRTI at about 1 per second, via Kalshi's authenticated CF Benchmarks value feed or whatever BRTI source we already have.
   - Message formats the page accepts on `?ws=wss://HOST/PATH`:
     - `{"type":"tick","venue":"polymarket","price":77439.2,"ts":1767225600}` (`ts` in seconds or ms; omit `venue` to feed both)
     - `{"type":"tick","venue":"kalshi","price":77437.9,"ts":1767225600}`
     - `{"type":"strike","venue":"kalshi","windowStartSec":1767225600,"price":77441.71}` (authoritative target from the Kalshi market when it opens; check the market object for its strike/target field)
     - `{"type":"strike","venue":"polymarket","windowStartSec":1767225600,"price":77441.71}` (Chainlink price at open)
     - `{"type":"model","venue":"polymarket","pUp":0.54}` (optional Champion probability per venue)
3. **Backfill on load.** If the page opens mid-window it only has ticks from that moment, and the target shows "TARGET EST". To fix it, call `window.rocket.seed('polymarket', [{t,p},...])` and `window.rocket.seed('kalshi', [...])` with ticks since the window start (and for Kalshi, the 60s before it). Oldest first. Also call `window.rocket.setStrike(venue, windowStartSec, price)` when known.
4. **Clock.** The page syncs to `serverTimeMs` from `GET /api/btc-15m/telemetry` (already added in `strategyGateRoutes.js`). Make sure the boundary logic uses server time, not the phone clock.
5. Pages: `/rocket` shows both charts stacked. `/rocket?venue=polymarket` or `?venue=kalshi` shows one full-height. There is no extra UI to hide.
6. Keep `window.tick(price)` working (feeds both venues) so nothing else breaks, but switch our feed to per-venue `rocket.tick`.

## Acceptance checks (report results honestly)
- [ ] At :00/:15/:30/:45 both charts reset and the countdown restarts at 15:00.
- [ ] Polymarket target equals the Chainlink price at open; Kalshi target equals the 60s BRTI average before open. The two targets will differ by a few dollars; that is correct.
- [ ] Kalshi shows a faint raw BRTI line plus a bold 60s-average line, and an "AVG ZONE" over the last 60s.
- [ ] At close, the CLOSED UP/DOWN flash matches what each venue actually resolved (compare to the venue after settlement, at least 5 windows).
- [ ] No "DEMO" tag once real ticks arrive.
