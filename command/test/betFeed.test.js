'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { createBetFeed } = require('../src/engine/betFeed');

const T = 1767225600 + 100; // 100s into a window
test('keeps kalshi and polymarket bets separate and buckets by window', () => {
  const sent = [];
  const f = createBetFeed({ broadcast: (m) => sent.push(m), now: () => T });
  f.recordBet('kalshi', { id: 'k1', side: 'up', ts: T - 10, qty: 10, cents: 55, bot: 'a' });
  f.recordBet('polymarket', { id: 'p1', side: 'DOWN', ts: (T - 5) * 1000, qty: 5, cents: 48 });
  f.recordBet('kalshi', { id: 'old', side: 'UP', ts: T - 2000, qty: 1 });
  const c = f.current();
  assert.equal(c.kalshi.length, 1);
  assert.equal(c.polymarket.length, 1);
  assert.equal(c.kalshi[0].side, 'UP');
  assert.equal(c.polymarket[0].ts, T - 5);
  assert.equal(sent.length, 3);
});
test('same id updates (cancel) instead of duplicating', () => {
  const f = createBetFeed({ now: () => T });
  f.recordBet('kalshi', { id: 'k1', side: 'UP', ts: T, qty: 10, cents: 50 });
  f.recordBet('kalshi', { id: 'k1', side: 'UP', ts: T, qty: 10, cents: 50, status: 'cancelled' });
  const c = f.current();
  assert.equal(c.kalshi.length, 1);
  assert.equal(c.kalshi[0].status, 'cancelled');
});
test('rejects bad venue, side, or missing id', () => {
  const f = createBetFeed({ now: () => T });
  assert.throws(() => f.recordBet('binance', { id: 1, side: 'UP' }), TypeError);
  assert.throws(() => f.recordBet('kalshi', { id: 1, side: 'LONG' }), TypeError);
  assert.throws(() => f.recordBet('kalshi', { side: 'UP' }), TypeError);
});
