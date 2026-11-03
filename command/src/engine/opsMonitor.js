'use strict';
// Liveness + readiness + trade log for the 15m desk. Zero dependencies (CommonJS).
// Heartbeats alone are not enough: a loop can be "alive" and do nothing. Every component can report a
// PROGRESS counter (ticks received, trades placed, rounds graded). If beats keep coming but progress
// is frozen past idleSec, the component is a ZOMBIE.

const DEFAULTS = {
  'kalshi-feed':     { label: 'Kalshi feed',     emoji: '🛰️', staleSec: 5,   idleSec: 30 },
  'polymarket-feed': { label: 'Polymarket feed', emoji: '📡', staleSec: 5,   idleSec: 30 },
  'kalshi-exec':     { label: 'Kalshi exec',     emoji: '⚔️', staleSec: 30,  idleSec: null },
  'polymarket-exec': { label: 'Polymarket exec', emoji: '🗡️', staleSec: 30,  idleSec: null },
  'shadow-engine':   { label: 'Shadow engine',   emoji: '👻', staleSec: 30,  idleSec: 1200 }, // must place or grade something every ~20 min
  'brain':           { label: 'Brain / learning', emoji: '🧠', staleSec: 120, idleSec: 3600 },
  'grader':          { label: 'Grader',          emoji: '📝', staleSec: 60,  idleSec: 1500 },
};
const REQUIRED_PROBES = ['creds', 'api', 'market', 'balance', 'strike'];
const PROBE_LABELS = [['creds', 'Credentials'], ['api', 'Exchange API'], ['market', 'Open 15m market'], ['balance', 'Balance'],
  ['strike', 'Round target'], ['clock', 'Clock sync'], ['risk', 'Risk limits'], ['armed', 'Armed']];

const normTs = (t, now) => { t = Number(t); if (!isFinite(t) || t <= 0) return now; return t > 1e11 ? t / 1000 : t; };

function createOpsMonitor(opts = {}) {
  const now = opts.now || (() => Date.now() / 1000);
  const defs = { ...DEFAULTS, ...(opts.components || {}) };
  const gateTarget = opts.gateTarget || 150;
  const onDead = opts.onDead || (() => {});
  const st = {}, trades = new Map(), events = [], down = new Set();
  const ev = (type, name, detail) => { events.unshift({ ts: now(), type, name, detail: detail || '' }); if (events.length > 60) events.pop(); if (opts.onEvent) opts.onEvent(events[0]); };

  function beat(name, info = {}) {
    const t = now();
    const s = st[name] || (st[name] = { last: t, progress: undefined, progressAt: t, crashes: 0, reported: 'ok', detail: '' });
    s.last = t; s.reported = info.status || 'ok';
    if (info.detail !== undefined) s.detail = String(info.detail).slice(0, 200);
    if (info.progress === undefined || info.progress !== s.progress) s.progressAt = t;
    if (info.progress !== undefined) s.progress = info.progress;
  }
  function crash(name, err) {
    const s = st[name] || (st[name] = { last: now(), progress: undefined, progressAt: now(), crashes: 0, reported: 'ok', detail: '' });
    s.crashes++; s.reported = 'error'; s.detail = String((err && err.message) || err).slice(0, 200);
    ev('crash', name, s.detail);
  }
  function statusOf(name) {
    const d = defs[name] || { staleSec: 60, idleSec: null }, s = st[name];
    if (!s) return 'UNKNOWN';
    const t = now(), age = t - s.last;
    if (age > d.staleSec * 3) return 'DEAD';
    if (d.idleSec && t - s.progressAt > d.idleSec) return 'ZOMBIE';
    if (s.reported === 'error') return 'ERROR';
    if (age > d.staleSec) return 'STALE';
    return 'ALIVE';
  }
  function check() {
    const out = {};
    for (const name of Object.keys(defs)) {
      const status = (out[name] = statusOf(name));
      if ((status === 'DEAD' || status === 'ZOMBIE') && !down.has(name)) { down.add(name); ev(status.toLowerCase(), name); try { onDead(name, status); } catch (_) {} }
      else if (status === 'ALIVE' && down.has(name)) { down.delete(name); ev('recovered', name); }
    }
    return out;
  }
  let timer = null;
  function start(ms = 2000) { if (!timer) { timer = setInterval(check, ms); if (timer.unref) timer.unref(); } return () => { clearInterval(timer); timer = null; }; }

  // ---- trades (live and shadow, both venues) ----
  function recordTrade(tr) {
    if (!tr || tr.id == null) throw new TypeError('trade.id required');
    if (tr.venue !== 'kalshi' && tr.venue !== 'polymarket') throw new TypeError('venue must be kalshi|polymarket');
    const id = String(tr.id), prev = trades.get(id) || {};
    const rec = { ...prev, id, venue: tr.venue, mode: tr.mode === 'shadow' ? 'shadow' : 'live',
      side: String(tr.side).toUpperCase() === 'DOWN' ? 'DOWN' : 'UP', qty: Number(tr.qty) || 0,
      cents: tr.cents == null ? null : Number(tr.cents), ts: normTs(tr.ts, now()), bot: tr.bot || prev.bot || null,
      status: tr.status === 'cancelled' ? 'cancelled' : 'open',
      result: tr.result ? String(tr.result).toUpperCase() : prev.result || null, pnl: tr.pnl != null ? Number(tr.pnl) : (prev.pnl == null ? null : prev.pnl) };
    trades.delete(id); trades.set(id, rec);
    while (trades.size > 400) trades.delete(trades.keys().next().value);
    return rec;
  }
  const gradeTrade = (id, g) => { const p = trades.get(String(id)); if (!p) return null; return recordTrade({ ...p, result: g.result, pnl: g.pnl }); };
  const list = () => [...trades.values()];
  function stats(mode) {
    const a = list().filter((x) => x.mode === mode && x.status !== 'cancelled');
    const wins = a.filter((x) => x.result === 'WIN').length, losses = a.filter((x) => x.result === 'LOSS').length;
    return { n: a.length, open: a.length - wins - losses, graded: wins + losses, wins, losses, pnl: a.reduce((s, x) => s + (x.pnl || 0), 0) };
  }
  function lastTradeAgeSec(venue, mode) {
    let best = null;
    for (const x of list()) if (x.venue === venue && x.mode === mode && x.status !== 'cancelled' && (best == null || x.ts > best)) best = x.ts;
    return best == null ? null : now() - best;
  }
  function snapshot() {
    const t = now();
    const components = Object.keys(defs).map((n) => { const s = st[n]; return { name: n, label: defs[n].label, emoji: defs[n].emoji, status: statusOf(n),
      ageSec: s ? t - s.last : null, progressAgeSec: s ? t - s.progressAt : null, crashes: s ? s.crashes : 0, detail: s ? s.detail : '' }; });
    return {
      serverTimeMs: Math.round(t * 1000), components,
      alerts: components.filter((c) => ['DEAD', 'ZOMBIE', 'ERROR'].includes(c.status)).map((c) => `${c.emoji} ${c.label} ${c.status}`),
      trades: list().sort((a, b) => b.ts - a.ts).slice(0, 40),
      stats: { live: stats('live'), shadow: { ...stats('shadow'), gateTarget } },
      lastTradeAgeSec: { kalshi: { live: lastTradeAgeSec('kalshi', 'live'), shadow: lastTradeAgeSec('kalshi', 'shadow') },
                         polymarket: { live: lastTradeAgeSec('polymarket', 'live'), shadow: lastTradeAgeSec('polymarket', 'shadow') } },
      events: events.slice(0, 12),
    };
  }
  return { beat, crash, statusOf, check, start, recordTrade, gradeTrade, stats, lastTradeAgeSec, snapshot, now, events: () => events.slice() };
}

// Wrap a loop so a crash restarts it with backoff instead of silently dying. fn(ctx) should run until stopped.
function supervise(name, fn, opts = {}) {
  const mon = opts.monitor, sleep = opts.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const maxBackoff = opts.maxBackoffMs || 60000, healthyMs = opts.healthyMs || 60000;
  let stopped = false, backoff = opts.initialBackoffMs || 1000, restarts = 0;
  const ctx = { beat: (info) => mon && mon.beat(name, info), isStopped: () => stopped };
  (async () => {
    while (!stopped) {
      const t0 = Date.now();
      try { await fn(ctx); if (mon) mon.beat(name, { status: 'error', detail: 'loop returned unexpectedly' }); }
      catch (e) { if (mon) mon.crash(name, e); }
      if (stopped) break;
      if (Date.now() - t0 > healthyMs) backoff = opts.initialBackoffMs || 1000;
      restarts++; await sleep(backoff); backoff = Math.min(backoff * 2, maxBackoff);
    }
  })();
  return { stop() { stopped = true; }, restarts: () => restarts };
}

// Grade one finished round for one venue. up = did it settle Up/Yes by that venue's own rule.
function gradeRound(trades, up, o = {}) {
  const fee = o.feeRate == null ? 0.015 : o.feeRate;
  return trades.filter((t) => t.status !== 'cancelled').map((t) => {
    const win = (t.side === 'UP') === !!up, q = t.qty || 0, c = (t.cents == null ? 50 : t.cents) / 100;
    return { id: t.id, result: win ? 'WIN' : 'LOSS', pnl: (win ? q * (1 - c) : -q * c) - fee * c * (1 - c) * q };
  });
}

const withTimeout = (fn, ms) => new Promise((res, rej) => {
  const t = setTimeout(() => rej(new Error('probe timed out')), ms);
  Promise.resolve().then(fn).then((v) => { clearTimeout(t); res(v); }, (e) => { clearTimeout(t); rej(e); });
});
const humanAge = (s) => (s < 60 ? Math.floor(s) + 's' : s < 3600 ? Math.floor(s / 60) + 'm' : s < 86400 ? Math.floor(s / 3600) + 'h' : Math.floor(s / 86400) + 'd');

// Preflight for one venue. probes[id]() returns {ok, warn?, detail}. Required probes that are not wired FAIL (not verified != ready).
async function runPreflight(mon, venue, probes = {}, o = {}) {
  const checks = [], add = (id, label, level, detail) => checks.push({ id, label, level, detail: detail || '' });
  const feed = mon.statusOf(venue + '-feed'); add('feed', 'Price feed', feed === 'ALIVE' ? 'pass' : 'fail', feed);
  const ex = mon.statusOf(venue + '-exec'); add('exec', 'Exec loop', ex === 'ALIVE' ? 'pass' : ex === 'STALE' ? 'warn' : 'fail', ex);
  for (const [id, label] of PROBE_LABELS) {
    if (!probes[id]) { add(id, label, REQUIRED_PROBES.includes(id) ? 'fail' : 'warn', 'probe not wired'); continue; }
    try { const r = await withTimeout(probes[id], o.timeoutMs || 4000); add(id, label, r && r.ok ? 'pass' : r && r.warn ? 'warn' : 'fail', r && r.detail); }
    catch (e) { add(id, label, 'fail', e.message); }
  }
  const age = mon.lastTradeAgeSec(venue, 'live'), thr = o.idleWarnSec || 6 * 3600;
  add('lastTrade', 'Last live trade', age == null || age > thr ? 'warn' : 'pass', age == null ? 'none recorded' : humanAge(age) + ' ago');
  const sh = mon.statusOf('shadow-engine'); add('shadow', 'Shadow engine', sh === 'ALIVE' ? 'pass' : 'warn', sh);
  return { venue, ready: !checks.some((c) => c.level === 'fail'), checkedAt: Date.now(), checks };
}

module.exports = { createOpsMonitor, supervise, gradeRound, runPreflight, DEFAULTS, REQUIRED_PROBES };
