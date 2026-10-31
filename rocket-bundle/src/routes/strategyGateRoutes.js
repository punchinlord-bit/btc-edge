'use strict';
const fs = require('fs');
const path = require('path');
const express = require('express');
const { evaluateAndPromote } = require('../engine/promotionGate');

const MARKET = 'btc_15m';

// ---- registries: Supabase if you pass a client, otherwise a JSON file fallback ----
function createJsonRegistry(file = path.join(process.cwd(), 'data', 'strategy_registry.json')) {
  const load = () => { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return { versions: [] }; } };
  const store = (d) => { fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, JSON.stringify(d, null, 2)); };
  return {
    async getChampion() { return load().versions.find((v) => v.status === 'CHAMPION') || null; },
    async getLatestChallenger() { return load().versions.filter((v) => v.status === 'CHALLENGER').pop() || null; },
    async save(records) { // single write = atomic enough for a JSON file
      const d = load();
      for (const r of records) { const i = d.versions.findIndex((v) => v.version_id === r.version_id); i >= 0 ? (d.versions[i] = r) : d.versions.push(r); }
      store(d);
    },
  };
}

function createSupabaseRegistry(supabase) {
  const row = (r) => r && ({ ...r, config: r.config_json });
  const toDb = (r) => ({ version_id: r.version_id, market_key: MARKET, parent_version_id: r.parent_version_id || null, status: r.status,
    config_json: r.config || r.config_json, metrics_json: r.metrics_json || {}, gate_checks_json: r.gate_checks_json || {},
    evaluated_at: r.evaluated_at || new Date().toISOString(), promoted_at: r.promoted_at || null });
  return {
    async getChampion() { const { data } = await supabase.from('strategy_versions').select('*').eq('market_key', MARKET).eq('status', 'CHAMPION').limit(1); return row(data && data[0]); },
    async getLatestChallenger() { const { data } = await supabase.from('strategy_versions').select('*').eq('market_key', MARKET).eq('status', 'CHALLENGER').order('evaluated_at', { ascending: false }).limit(1); return row(data && data[0]); },
    async save(records) { const { error } = await supabase.from('strategy_versions').upsert(records.map(toDb)); if (error) throw error; }, // one statement
  };
}

// ---- router factory ----
// deps: { supabase?, registry?, broadcast(msgObj), getLiveState(), getOosWindows(), predict?, gateOptions? }
function createStrategyGateRoutes(deps = {}) {
  const registry = deps.registry || (deps.supabase ? createSupabaseRegistry(deps.supabase) : createJsonRegistry());
  const broadcast = deps.broadcast || (() => {});
  const getLiveState = deps.getLiveState || (() => ({}));
  const getOosWindows = deps.getOosWindows || (async () => []);
  let latestGate = null;

  // Operator readiness reports booleans only. Never return keys.
  const ops = [
    { id: 'op1', label: 'Operator 1 (Self)', kalshi: 'OP1_KALSHI_KEY_ID', poly: 'OP1_POLY_WALLET', armed: false },
    { id: 'op2', label: 'Operator 2 (Partner)', kalshi: 'OP2_KALSHI_KEY_ID', poly: 'OP2_POLY_WALLET', armed: false },
  ];
  const opView = (live) => ops.map((o) => ({
    id: o.id, label: o.label, armed: o.armed,
    kalshiReady: Boolean(process.env[o.kalshi]), polymarketReady: Boolean(process.env[o.poly]),
    signal: (live.championProbUp ?? 0.5) >= 0.5 ? 'UP' : 'DOWN',
  }));

  const router = express.Router();
  router.use(express.json({ limit: '2mb' }));

  router.get('/telemetry', async (_req, res) => {
    const now = Math.floor(Date.now() / 1000), start = Math.floor(now / 900) * 900;
    const live = { windowStartSec: start, windowEndSec: start + 900, strikePrice: null, currentPrice: null, championVersionId: null,
      championProbUp: null, challengerProbUp: null, orderbookImbalance: null, latestNewsSignal: null, ...(await getLiveState()) };
    const champ = await registry.getChampion(MARKET);
    res.json({ ...live, championVersionId: live.championVersionId || (champ && champ.version_id) || null,
      gate: latestGate, operators: opView(live), serverTime: now });
  });

  router.get('/gate/latest', (_req, res) => res.json(latestGate));

  // Body: optional { challenger: { version_id, config } }. Empty body = latest stored CHALLENGER.
  router.post('/gate/evaluate', async (req, res) => {
    try {
      const champion = await registry.getChampion(MARKET);
      if (!champion) return res.status(409).json({ error: 'No CHAMPION in strategy_versions. Seed one first.' });
      const challenger = (req.body && req.body.challenger) || await registry.getLatestChallenger(MARKET);
      if (!challenger) return res.status(404).json({ error: 'No challenger supplied or stored.' });
      const windows = await getOosWindows();
      const result = evaluateAndPromote(challenger, windows, champion, { ...(deps.gateOptions || {}), predict: deps.predict });
      await registry.save([result.challenger, result.champion]);
      latestGate = { at: new Date().toISOString(), challengerId: challenger.version_id, verdict: result.verdict, failed: result.failed, gates: result.gates };
      broadcast({ type: 'gate', result: latestGate });
      res.json(latestGate);
    } catch (e) { res.status(400).json({ error: e.message }); }
  });

  router.post('/operators/:id/arm', (req, res) => {
    const o = ops.find((x) => x.id === req.params.id);
    if (!o) return res.status(404).json({ error: 'unknown operator' });
    o.armed = Boolean(req.body && req.body.armed);
    broadcast({ type: 'operators', operators: opView({}) });
    res.json({ id: o.id, armed: o.armed });
  });

  return router;
}

module.exports = { createStrategyGateRoutes, createJsonRegistry, createSupabaseRegistry };
// Mount: app.use('/api/btc-15m', createStrategyGateRoutes({ supabase, broadcast, getLiveState, getOosWindows, predict }));
