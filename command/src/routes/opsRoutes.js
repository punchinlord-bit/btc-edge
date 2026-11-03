'use strict';
const crypto = require('crypto');
const express = require('express');
const { runPreflight } = require('../engine/opsMonitor');

// Mount: app.use('/api/btc-15m', createOpsRoutes(ops, { probes, broadcast }))
// probes = { kalshi: { creds, api, market, balance, strike, clock?, risk?, armed? }, polymarket: { ...same } }
// Each probe is async () => ({ ok, warn?, detail }). Probes must be READ-ONLY. Never place or cancel orders in a probe.
function createOpsRoutes(ops, { probes = {}, broadcast = () => {}, opsKey = process.env.OPS_KEY } = {}) {
  const router = express.Router();
  router.use(express.json({ limit: '256kb' }));
  let cache = { at: 0, val: null };

  const auth = (req, res, next) => {
    if (opsKey) {
      const a = Buffer.from(String(req.get('x-ops-key') || '')), b = Buffer.from(opsKey);
      if (a.length === b.length && crypto.timingSafeEqual(a, b)) return next();
      return res.status(401).json({ error: 'bad x-ops-key' });
    }
    const ip = req.ip || '';
    if (ip === '127.0.0.1' || ip === '::1' || ip === '::ffff:127.0.0.1') return next();
    return res.status(401).json({ error: 'set OPS_KEY to accept remote writes' });
  };

  router.get('/ops', async (_req, res) => {
    if (!cache.val || Date.now() - cache.at > 10000) {
      const [kalshi, polymarket] = await Promise.all(['kalshi', 'polymarket'].map((v) => runPreflight(ops, v, probes[v] || {})));
      cache = { at: Date.now(), val: { kalshi, polymarket } };
    }
    res.json({ ...ops.snapshot(), readiness: cache.val });
  });
  router.post('/ops/heartbeat', auth, (req, res) => {
    const b = req.body || {};
    if (!b.name) return res.status(400).json({ error: 'name required' });
    ops.beat(String(b.name), { status: b.status, detail: b.detail, progress: b.progress });
    res.json({ ok: true });
  });
  router.post('/ops/trade', auth, (req, res) => {
    try { const r = ops.recordTrade(req.body); broadcast({ type: 'ops-trade', trade: r }); res.json(r); }
    catch (e) { res.status(400).json({ error: e.message }); }
  });
  router.post('/ops/trade/:id/grade', auth, (req, res) => {
    const r = ops.gradeTrade(req.params.id, req.body || {});
    if (!r) return res.status(404).json({ error: 'unknown trade id' });
    broadcast({ type: 'ops-trade', trade: r }); res.json(r);
  });
  return router;
}
module.exports = { createOpsRoutes };
