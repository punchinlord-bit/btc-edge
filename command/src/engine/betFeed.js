'use strict';
// Per-venue bot bet feed for the rocket chart. Kalshi and Polymarket bets are kept separate.
// Call recordBet() from the execution layer when an order is placed, filled, or cancelled.
const WIN = 900;
const VENUES = ['kalshi', 'polymarket'];

function createBetFeed({ broadcast = () => {}, persist = null, onRecord = null, now = () => Date.now() / 1000 } = {}) {
  const byId = { kalshi: new Map(), polymarket: new Map() };

  function recordBet(venue, bet) {
    if (!VENUES.includes(venue)) throw new TypeError("venue must be 'kalshi' or 'polymarket'");
    if (!bet || bet.id == null) throw new TypeError('bet.id is required (use the exchange order id)');
    const side = String(bet.side || '').toUpperCase();
    if (side !== 'UP' && side !== 'DOWN') throw new TypeError("bet.side must be 'UP' or 'DOWN'");
    let ts = Number(bet.ts == null ? now() : bet.ts);
    if (ts > 1e12) ts /= 1000;
    const rec = {
      id: String(bet.id), venue, side, ts,
      qty: Number(bet.qty) || 0,                                   // contracts
      cents: bet.cents == null ? null : Number(bet.cents),         // price paid per contract, in cents
      bot: bet.bot || null,
      mode: bet.mode === 'shadow' ? 'shadow' : 'live',            // shadow = paper trades from the shadow engine
      result: bet.result || null, pnl: bet.pnl == null ? null : Number(bet.pnl),
      status: bet.status === 'cancelled' ? 'cancelled' : 'open',
      windowStartSec: Math.floor(ts / WIN) * WIN,
    };
    byId[venue].set(rec.id, rec);                                  // same id = update (fill, cancel)
    const cutoff = Math.floor(now() / WIN) * WIN - 4 * WIN;        // keep ~1 hour
    for (const m of Object.values(byId)) for (const [k, v] of m) if (v.windowStartSec < cutoff) m.delete(k);
    broadcast({ type: 'bet', ...rec });
    if (persist) Promise.resolve(persist(rec)).catch(() => {});
    if (onRecord) { try { onRecord(rec); } catch (_) {} }
    return rec;
  }

  // Current-window bets per venue, for page backfill and reconciliation.
  function current() {
    const ws = Math.floor(now() / WIN) * WIN;
    const pick = (v) => [...byId[v].values()].filter((b) => b.windowStartSec === ws);
    return { windowStartSec: ws, kalshi: pick('kalshi'), polymarket: pick('polymarket') };
  }
  return { recordBet, current };
}

module.exports = { createBetFeed, VENUES, WIN };
