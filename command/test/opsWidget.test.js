'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { model } = require('../public/js/opsWidget.js');

const snap = {
  serverTimeMs: 1767225700000,
  components: [{ name: 'shadow-engine', status: 'ZOMBIE', ageSec: 3, progressAgeSec: 1500, crashes: 2 }, { name: 'kalshi-feed', status: 'ALIVE', ageSec: 1, progressAgeSec: 1, crashes: 0 }],
  alerts: ['👻 Shadow engine ZOMBIE'],
  trades: [{ id: 'a', venue: 'kalshi', mode: 'shadow', side: 'UP', qty: 10, cents: 55, ts: 1767225690, bot: 'x', status: 'open', result: 'WIN', pnl: 4.5 },
           { id: 'b', venue: 'polymarket', mode: 'live', side: 'DOWN', qty: 3, cents: 40, ts: 1767225680, status: 'open' },
           { id: 'c', venue: 'kalshi', mode: 'live', side: 'UP', qty: 3, cents: 40, ts: 1767225670, status: 'cancelled' }],
  stats: { shadow: { graded: 150, wins: 80, losses: 70, pnl: 12.5, gateTarget: 150 } },
  lastTradeAgeSec: { kalshi: { live: 3 * 86400, shadow: 10 }, polymarket: { live: null, shadow: 12 } },
  readiness: { kalshi: { ready: true, checks: [] }, polymarket: { ready: false, checks: [{ level: 'fail' }, { level: 'warn' }] } },
};
test('view-model: tiles, zombie text, readiness, sleepy flags, XP unlock, trade rows', () => {
  const m = model(snap, 0);
  assert.equal(m.tiles.length, 7);
  const z = m.tiles.find((t) => t.name === 'shadow-engine');
  assert.equal(z.status, 'ZOMBIE'); assert.equal(z.idleTxt, 'idle 25m'); assert.equal(m.tiles.find((t) => t.name === 'brain').status, 'UNKNOWN');
  assert.equal(m.players.kalshi.ready, true); assert.equal(m.players.kalshi.sleepy, true); assert.equal(m.players.kalshi.liveTxt, '3d ago');
  assert.equal(m.players.polymarket.ready, false); assert.equal(m.players.polymarket.fails, 1); assert.equal(m.players.polymarket.liveTxt, 'never');
  assert.equal(m.xp.unlocked, true); assert.equal(m.xp.pct, 100);
  assert.deepEqual(m.rows.map((r) => r.state), ['win', 'open', 'cancel']);
  assert.equal(m.rows[0].res, '🏆 +$4.50'); assert.equal(m.rows[0].mem, '👻'); assert.equal(m.rows[1].sem, '🔻 DOWN'); assert.equal(m.rows[0].ago, '10s');
});
test('ages keep counting between polls', () => {
  const m = model(snap, 65);
  assert.equal(m.rows[0].ago, '1m'); assert.equal(m.tiles.find((t) => t.name === 'kalshi-feed').ageTxt, '1m');
});
