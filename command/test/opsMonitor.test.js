'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createOpsMonitor, supervise, gradeRound, runPreflight } = require('../src/engine/opsMonitor');
const { createBetFeed } = require('../src/engine/betFeed');

test('feed goes ALIVE -> STALE -> DEAD and onDead fires once', () => {
  let T = 1000; const dead = [];
  const m = createOpsMonitor({ now: () => T, onDead: (n, s) => dead.push([n, s]) });
  assert.equal(m.statusOf('kalshi-feed'), 'UNKNOWN');
  m.beat('kalshi-feed', { progress: 1 }); T += 4; assert.equal(m.statusOf('kalshi-feed'), 'ALIVE');
  T += 2; assert.equal(m.statusOf('kalshi-feed'), 'STALE');
  T += 10; m.check(); m.check();
  assert.equal(m.statusOf('kalshi-feed'), 'DEAD');
  assert.deepEqual(dead, [['kalshi-feed', 'DEAD']]);
});

test('ZOMBIE: heartbeats keep coming but progress is frozen; recovers when progress moves', () => {
  let T = 1000; const m = createOpsMonitor({ now: () => T });
  for (let i = 0; i < 140; i++) { m.beat('shadow-engine', { progress: 7 }); T += 10; }   // 1400s, no progress
  assert.equal(m.statusOf('shadow-engine'), 'ZOMBIE');
  assert.ok(m.snapshot().alerts.some((a) => a.includes('ZOMBIE')));
  m.beat('shadow-engine', { progress: 8 });
  assert.equal(m.statusOf('shadow-engine'), 'ALIVE');
});

test('supervise restarts a crashing loop with backoff and counts crashes', async () => {
  const m = createOpsMonitor(); let runs = 0, done;
  const finished = new Promise((r) => { done = r; });
  const sup = supervise('brain', async (ctx) => {
    runs++; if (runs < 3) throw new Error('boom ' + runs);
    ctx.beat({ progress: 1 }); done(); await new Promise(() => {});
  }, { monitor: m, sleep: () => Promise.resolve() });
  await finished; sup.stop();
  assert.equal(runs, 3);
  assert.equal(m.snapshot().components.find((c) => c.name === 'brain').crashes, 2);
  assert.equal(m.statusOf('brain'), 'ALIVE');
  assert.ok(sup.restarts() >= 2);
});

test('preflight: unwired required probes are NOT ready; all-green is ready; a failing probe blocks', async () => {
  let T = 5000; const m = createOpsMonitor({ now: () => T });
  m.beat('kalshi-feed', { progress: 1 }); m.beat('kalshi-exec');
  const ok = async () => ({ ok: true, detail: 'ok' });
  let r = await runPreflight(m, 'kalshi', {});
  assert.equal(r.ready, false);
  assert.ok(r.checks.find((c) => c.id === 'creds').detail.includes('not wired'));
  const all = { creds: ok, api: ok, market: ok, balance: ok, strike: ok };
  r = await runPreflight(m, 'kalshi', all);
  assert.equal(r.ready, true);
  assert.equal(r.checks.find((c) => c.id === 'lastTrade').level, 'warn');     // never traded -> flagged idle
  r = await runPreflight(m, 'kalshi', { ...all, api: async () => ({ ok: false, detail: '401' }) });
  assert.equal(r.ready, false);
  r = await runPreflight(m, 'kalshi', { ...all, balance: () => new Promise(() => {}) }, { timeoutMs: 20 });
  assert.equal(r.checks.find((c) => c.id === 'balance').detail, 'probe timed out');
  const poly = await runPreflight(m, 'polymarket', all);
  assert.equal(poly.ready, false);                                              // polymarket feed never beat
});

test('trades: shadow vs live stats, grading, dedupe by id, last-trade age', () => {
  let T = 10000; const m = createOpsMonitor({ now: () => T, gateTarget: 150 });
  m.recordTrade({ id: 's1', venue: 'kalshi', mode: 'shadow', side: 'up', qty: 10, cents: 50, ts: T - 100 });
  m.recordTrade({ id: 's2', venue: 'polymarket', mode: 'shadow', side: 'DOWN', qty: 10, cents: 40, ts: T - 50 });
  m.recordTrade({ id: 'l1', venue: 'kalshi', mode: 'live', side: 'UP', qty: 5, cents: 60, ts: T - 30 });
  const graded = gradeRound([m.snapshot().trades.find((t) => t.id === 's1'), m.snapshot().trades.find((t) => t.id === 's2')], true, { feeRate: 0 });
  assert.deepEqual(graded.map((g) => g.result), ['WIN', 'LOSS']);
  graded.forEach((g) => m.gradeTrade(g.id, g));
  const s = m.snapshot();
  assert.equal(s.stats.shadow.graded, 2); assert.equal(s.stats.shadow.wins, 1); assert.equal(s.stats.shadow.gateTarget, 150);
  assert.ok(Math.abs(s.stats.shadow.pnl - (5 - 4)) < 1e-9);
  assert.equal(s.stats.live.n, 1);
  assert.equal(Math.round(s.lastTradeAgeSec.kalshi.live), 30);
  m.recordTrade({ id: 'l1', venue: 'kalshi', mode: 'live', side: 'UP', qty: 5, cents: 60, ts: T - 30, status: 'cancelled' });
  assert.equal(m.snapshot().stats.live.n, 0);
  assert.equal(m.snapshot().trades.filter((t) => t.id === 'l1').length, 1);
  assert.throws(() => m.recordTrade({ id: 'x', venue: 'binance', side: 'UP' }), TypeError);
});

test('betFeed passes mode through and calls onRecord so chart and ops log share one call', () => {
  const seen = [];
  const f = createBetFeed({ onRecord: (r) => seen.push(r), now: () => 1767225700 });
  f.recordBet('kalshi', { id: 'sh-1', side: 'UP', ts: 1767225690, qty: 3, cents: 50, mode: 'shadow' });
  assert.equal(seen[0].mode, 'shadow');
  assert.equal(f.current().kalshi[0].mode, 'shadow');
});
