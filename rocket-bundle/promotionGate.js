'use strict';
// Champion vs Challenger promotion gate. Zero dependencies (CommonJS).
// If package.json has "type":"module", rename to .cjs or convert to ESM exports.

const DEFAULTS = {
  minWindows: 150,        // Gate 1: OOS sample floor
  brierMargin: 0.005,     // Gate 2: challenger Brier must beat champion by this
  evMarginBps: 125,       // Gate 3: net EV edge over champion (bps of a $1 contract)
  regimeFloor: 0.515,     // Gate 4: win rate floor in EVERY regime
  minRegimeBets: 10,      // Gate 4: bets needed per regime to count
  jitter: 0.10,           // Gate 5: +/-10% parameter perturbation
  jitterRetention: 0.75,  // Gate 5: worst corner must keep 75% of baseline EV
  defaultMinEdge: 0.02,   // min net edge needed to place a bet
};
const REGIMES = ['chop', 'trend', 'spike'];
const sigmoid = (z) => 1 / (1 + Math.exp(-z));

// Default predictor: logistic over config.weights * features + config.bias.
// Replace via opts.predict(config, features) -> pUp with your real model.
function defaultPredict(cfg, f) {
  let z = Number(cfg.bias) || 0;
  for (const [k, w] of Object.entries(cfg.weights || {})) z += w * (Number(f && f[k]) || 0);
  return sigmoid(z);
}
const fee = (rate, ask) => rate * ask * (1 - ask);

function validateWindows(ws) {
  if (!Array.isArray(ws)) throw new TypeError('oosWindows must be an array');
  ws.forEach((w, i) => {
    if (w.outcome !== 0 && w.outcome !== 1) throw new TypeError(`window ${i}: outcome must be 0|1`);
    if (!REGIMES.includes(w.regime)) throw new TypeError(`window ${i}: bad regime`);
    for (const k of ['askUp', 'askDown']) {
      if (!(w[k] > 0 && w[k] < 1)) throw new TypeError(`window ${i}: ${k} must be in (0,1)`);
    }
  });
}
function validateStrategy(s, label) {
  if (!s || typeof s.version_id !== 'string' || !s.config || typeof s.config !== 'object') {
    throw new TypeError(`${label} needs { version_id: string, config: object }`);
  }
}

function simulate(cfg, ws, predict) {
  const minEdge = typeof cfg.minEdge === 'number' ? cfg.minEdge : DEFAULTS.defaultMinEdge;
  const by = Object.fromEntries(REGIMES.map((r) => [r, { bets: 0, wins: 0 }]));
  let brier = 0, pnl = 0, bets = 0, wins = 0;
  for (const w of ws) {
    const p = Math.min(0.999, Math.max(0.001, predict(cfg, w.features || {})));
    brier += (p - w.outcome) ** 2;
    const rate = w.feeRate == null ? 0.015 : w.feeRate;
    const eUp = p - w.askUp - fee(rate, w.askUp);
    const eDn = 1 - p - w.askDown - fee(rate, w.askDown);
    const side = eUp >= eDn ? 'up' : 'down';
    if (Math.max(eUp, eDn) < minEdge) continue;
    const ask = side === 'up' ? w.askUp : w.askDown;
    const won = side === 'up' ? w.outcome === 1 : w.outcome === 0;
    pnl += (won ? 1 - ask : -ask) - fee(rate, ask);
    bets++; by[w.regime].bets++;
    if (won) { wins++; by[w.regime].wins++; }
  }
  const n = ws.length || 1;
  const byRegime = {};
  for (const r of REGIMES) byRegime[r] = { ...by[r], winRate: by[r].bets ? by[r].wins / by[r].bets : null };
  return { brier: brier / n, netEv: pnl / n, bets, winRate: bets ? wins / bets : null, byRegime };
}

// 8 deterministic +/- sign patterns across all numeric params ("corners" of the jitter box).
function sign(c, i, n) {
  switch (c) {
    case 0: return 1;
    case 1: return -1;
    case 2: return i % 2 ? -1 : 1;
    case 3: return i % 2 ? 1 : -1;
    case 4: return i < n / 2 ? 1 : -1;
    case 5: return i < n / 2 ? -1 : 1;
    case 6: return i % 3 === 0 ? -1 : 1;
    default: return i % 3 === 0 ? 1 : -1;
  }
}
function perturb(cfg, corner, pct) {
  let leaves = 0;
  const count = (v) => {
    if (typeof v === 'number') leaves++;
    else if (Array.isArray(v)) v.forEach(count);
    else if (v && typeof v === 'object') Object.keys(v).forEach((k) => count(v[k]));
  };
  count(cfg);
  let i = 0;
  const walk = (v) => {
    if (typeof v === 'number') return v * (1 + pct * sign(corner, i++, leaves));
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === 'object') {
      const o = {};
      for (const k of Object.keys(v).sort()) o[k] = walk(v[k]);
      return o;
    }
    return v;
  };
  return walk(cfg);
}

// Pure function: returns the records to persist. It never touches the DB itself.
function evaluateAndPromote(challenger, oosWindows, champion, opts = {}) {
  const o = { ...DEFAULTS, ...opts };
  const predict = opts.predict || defaultPredict;
  validateStrategy(challenger, 'challenger');
  validateStrategy(champion, 'champion');
  validateWindows(oosWindows);

  const ch = simulate(challenger.config, oosWindows, predict);
  const cp = simulate(champion.config, oosWindows, predict);
  const worst = Math.min(...Array.from({ length: 8 }, (_, c) =>
    simulate(perturb(challenger.config, c, o.jitter), oosWindows, predict).netEv));
  const retention = ch.netEv > 0 ? worst / ch.netEv : 0;

  const rr = REGIMES.map((r) => ({ r, ...ch.byRegime[r] }));
  const minRegime = rr.reduce((a, b) => ((a.winRate ?? 0) <= (b.winRate ?? 0) ? a : b));
  const regimePass = rr.every((x) => x.bets >= o.minRegimeBets && x.winRate >= o.regimeFloor);
  const evBps = (ch.netEv - cp.netEv) * 10000;

  const gates = {
    sample: { pass: oosWindows.length >= o.minWindows, value: oosWindows.length, threshold: o.minWindows },
    brier: { pass: cp.brier - ch.brier >= o.brierMargin, challenger: ch.brier, champion: cp.brier, improvement: cp.brier - ch.brier, threshold: o.brierMargin },
    ev: { pass: evBps >= o.evMarginBps, challengerEv: ch.netEv, championEv: cp.netEv, edgeBps: evBps, threshold: o.evMarginBps },
    regime: { pass: regimePass, min: minRegime.winRate, minRegime: minRegime.r, byRegime: ch.byRegime, threshold: o.regimeFloor },
    jitter: { pass: retention >= o.jitterRetention, retention, worstEv: worst, baselineEv: ch.netEv, threshold: o.jitterRetention },
  };
  const promoted = Object.values(gates).every((g) => g.pass);
  const now = new Date().toISOString();
  return {
    promoted,
    verdict: promoted ? 'PROMOTED' : 'REJECTED',
    failed: Object.entries(gates).filter(([, g]) => !g.pass).map(([k]) => k),
    gates,
    metrics: { challenger: ch, champion: cp },
    challenger: { ...challenger, status: promoted ? 'CHAMPION' : 'REJECTED', parent_version_id: champion.version_id,
      metrics_json: { challenger: ch, champion: cp }, gate_checks_json: gates, evaluated_at: now, promoted_at: promoted ? now : null },
    champion: promoted ? { ...champion, status: 'ARCHIVED' } : champion,
  };
}

module.exports = { evaluateAndPromote, simulate, perturb, defaultPredict, DEFAULTS, REGIMES };
