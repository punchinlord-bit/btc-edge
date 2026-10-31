'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { evaluateAndPromote } = require('../src/engine/promotionGate');

function rng(seed) { // mulberry32
  let a = seed >>> 0;
  return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
// Feature s=+/-1 predicts outcome. chopHit = P(outcome==1 | s>0) in the chop regime.
function makeWindows(n = 450, { chopHit = 0.75, hit = 0.75, seed = 7 } = {}) {
  const r = rng(seed), out = [];
  for (let i = 0; i < n; i++) {
    const regime = i % 5 === 0 ? 'chop' : i % 5 <= 2 ? 'trend' : 'spike';
    const s = r() < 0.5 ? 1 : -1;
    const h = regime === 'chop' ? chopHit : hit;
    out.push({ outcome: r() < (s > 0 ? h : 1 - h) ? 1 : 0, regime, features: { s }, askUp: 0.51, askDown: 0.51, feeRate: 0.015 });
  }
  return out;
}
const linear = (cfg, f) => 0.5 + cfg.conf * f.s;                               // robust predictor
const magic = (cfg, f) => 0.5 + (Math.abs(cfg.a - 1) < 0.05 ? 0.3 : 0) * f.s;  // knife-edge predictor

test('rejects an overfit challenger that fails the +/-10% jitter test', () => {
  const res = evaluateAndPromote(
    { version_id: 'chal-magic', config: { a: 1, minEdge: 0.02 } }, makeWindows(),
    { version_id: 'champ-0', config: { a: 0, minEdge: 0.02 } }, { predict: magic });
  assert.equal(res.promoted, false);
  assert.deepEqual(res.failed, ['jitter']);
  assert.equal(res.challenger.status, 'REJECTED');
});

test('rejects high overall win rate when chop regime is below 51.5%', () => {
  const res = evaluateAndPromote(
    { version_id: 'chal-chop', config: { conf: 0.3, minEdge: 0.02 } }, makeWindows(450, { chopHit: 0.35 }),
    { version_id: 'champ-0', config: { conf: 0, minEdge: 0.02 } }, { predict: linear });
  assert.ok(res.metrics.challenger.winRate > 0.6, 'overall win rate looks great');
  assert.equal(res.gates.regime.pass, false);
  assert.equal(res.gates.regime.minRegime, 'chop');
  assert.equal(res.promoted, false);
});

test('rejects when Brier calibration degrades despite lucky raw PnL', () => {
  const res = evaluateAndPromote(
    { version_id: 'chal-overconf', config: { conf: 0.49, minEdge: 0.02 } }, makeWindows(),
    { version_id: 'champ-calibrated', config: { conf: 0.3, minEdge: 0.35 } }, { predict: linear });
  assert.equal(res.gates.ev.pass, true, 'raw EV looks better');
  assert.equal(res.gates.brier.pass, false);
  assert.equal(res.promoted, false);
});

test('promotes a genuinely superior challenger and archives the old champion', () => {
  const res = evaluateAndPromote(
    { version_id: 'chal-good', config: { conf: 0.3, minEdge: 0.02 } }, makeWindows(),
    { version_id: 'champ-old', config: { conf: 0.1, minEdge: 0.15 } }, { predict: linear });
  assert.equal(res.promoted, true, JSON.stringify(res.failed));
  assert.equal(res.challenger.status, 'CHAMPION');
  assert.equal(res.champion.status, 'ARCHIVED');
  assert.ok(res.challenger.promoted_at);
});

test('rejects when the OOS sample is below the floor', () => {
  const res = evaluateAndPromote(
    { version_id: 'c', config: { conf: 0.3 } }, makeWindows(60),
    { version_id: 'o', config: { conf: 0.1, minEdge: 0.15 } }, { predict: linear });
  assert.equal(res.gates.sample.pass, false);
  assert.equal(res.promoted, false);
});
