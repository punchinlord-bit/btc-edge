(function () {
  'use strict';
  // COMMAND DECK: live watchdog + readiness + trade feed. OpsWidget.mount(hostEl, { api:'' , demo:false })
  // Reads GET {api}/api/btc-15m/ops every 2s. If the backend is unreachable it SAYS SO (the watchdog must never fail silently).
  const ORDER = ['kalshi-feed', 'polymarket-feed', 'kalshi-exec', 'polymarket-exec', 'shadow-engine', 'brain', 'grader'];
  const SHORT = { 'kalshi-feed': 'K FEED', 'polymarket-feed': 'P FEED', 'kalshi-exec': 'K EXEC', 'polymarket-exec': 'P EXEC', 'shadow-engine': 'SHADOW', brain: 'BRAIN', grader: 'GRADER' };
  const EMO = { 'kalshi-feed': '🛰️', 'polymarket-feed': '📡', 'kalshi-exec': '⚔️', 'polymarket-exec': '🗡️', 'shadow-engine': '👻', brain: '🧠', grader: '📝' };
  const VEM = { kalshi: '🔷', polymarket: '🟣' }, VNM = { kalshi: 'KALSHI', polymarket: 'POLYMARKET' };
  const STXT = { ALIVE: 'ONLINE', STALE: 'LAGGING', DEAD: 'OFFLINE', ZOMBIE: 'NO PULSE', ERROR: 'ERROR', UNKNOWN: 'NO SIGNAL' };
  const SICO = { ALIVE: '', STALE: '⚠️', DEAD: '💀', ZOMBIE: '💤', ERROR: '🔥', UNKNOWN: '❔' };
  const CHK = { pass: '✔', warn: '⚠', fail: '✖' };
  const age = (s) => (s == null ? '--' : s < 60 ? Math.floor(s) + 's' : s < 3600 ? Math.floor(s / 60) + 'm' : s < 86400 ? Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm' : Math.floor(s / 86400) + 'd');
  const money = (v) => (v >= 0 ? '+' : '−') + '$' + Math.abs(v).toFixed(2);
  const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  // Pure view-model (tested without a DOM). elapsed = seconds since the snapshot was fetched.
  function model(snap, elapsed) {
    elapsed = elapsed || 0;
    const byName = {}; (snap.components || []).forEach((c) => { byName[c.name] = c; });
    const tiles = ORDER.map((n) => { const c = byName[n] || { name: n, status: 'UNKNOWN' };
      return { name: n, emoji: EMO[n], label: SHORT[n], status: c.status, ageTxt: c.ageSec == null ? '--' : age(c.ageSec + elapsed),
        idleTxt: c.status === 'ZOMBIE' ? 'idle ' + age((c.progressAgeSec || 0) + elapsed) : '', crashes: c.crashes || 0, detail: c.detail || '' }; });
    const players = {};
    ['kalshi', 'polymarket'].forEach((v) => {
      const r = (snap.readiness || {})[v] || { ready: false, checks: [] };
      const live = snap.lastTradeAgeSec && snap.lastTradeAgeSec[v] ? snap.lastTradeAgeSec[v] : {};
      const la = live.live == null ? null : live.live + elapsed, sa = live.shadow == null ? null : live.shadow + elapsed;
      players[v] = { ready: !!r.ready, checks: r.checks || [], liveTxt: la == null ? 'never' : age(la) + ' ago', shadowTxt: sa == null ? 'never' : age(sa) + ' ago',
        sleepy: la == null || la > 6 * 3600, fails: (r.checks || []).filter((c) => c.level === 'fail').length };
    });
    const nowSec = (snap.serverTimeMs || Date.now()) / 1000 + elapsed;
    const rows = (snap.trades || []).slice(0, 14).map((t) => ({ id: t.id, time: clock(t.ts), venue: t.venue, vem: VEM[t.venue], mode: t.mode,
      mem: t.mode === 'shadow' ? '👻' : '⚔️', side: t.side, sem: t.side === 'UP' ? '🚀 UP' : '🔻 DOWN',
      size: (t.qty || 0) + ' @ ' + (t.cents == null ? '?' : t.cents) + '¢' + (t.bot ? ' · ' + t.bot : ''),
      state: t.status === 'cancelled' ? 'cancel' : t.result === 'WIN' ? 'win' : t.result === 'LOSS' ? 'loss' : 'open',
      res: t.status === 'cancelled' ? '🚫' : t.result === 'WIN' ? '🏆 ' + money(t.pnl || 0) : t.result === 'LOSS' ? '💀 ' + money(t.pnl || 0) : '⏳',
      ago: age(Math.max(0, nowSec - t.ts)) }));
    const sh = (snap.stats && snap.stats.shadow) || { graded: 0, gateTarget: 150, wins: 0, losses: 0, pnl: 0, n: 0 };
    const xp = { graded: sh.graded, target: sh.gateTarget || 150, pct: Math.min(100, (sh.graded / (sh.gateTarget || 150)) * 100), wins: sh.wins, losses: sh.losses, pnl: sh.pnl, unlocked: sh.graded >= (sh.gateTarget || 150) };
    return { tiles, players, rows, xp, alerts: snap.alerts || [], events: snap.events || [] };
  }

  const CSS = `.ow{--g:#00f59b;--r:#ff3b69;--a:#f59e0b;--p:#a78bfa;--c:#38bdf8;position:relative;font:700 11px ui-monospace,"SF Mono",Menlo,Consolas,monospace;color:#cfe3ff;background:#080c15;border:1px solid #1d2a44;border-radius:14px;padding:10px;overflow:hidden;text-transform:uppercase;letter-spacing:.04em;box-shadow:0 0 24px #38bdf812}
.ow:after{content:"";position:absolute;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,#ffffff07 0 1px,transparent 1px 3px)}
.ow-title{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:900;color:#e8f3ff;text-shadow:0 0 8px var(--c)}.ow-sp{flex:1}
.ow-led{width:8px;height:8px;border-radius:50%;background:var(--g);box-shadow:0 0 8px var(--g);animation:ow-pulse 1.4s infinite}.ow-led.off{background:var(--r);box-shadow:0 0 8px var(--r);animation:ow-blink .5s infinite}
.ow-tag{font-size:9px;color:#8aa0c4}.ow-demo{color:var(--a);border:1px solid var(--a);border-radius:5px;padding:1px 5px;font-size:9px}
.ow-banner{margin:8px 0 2px;padding:6px 8px;border:1px solid var(--r);border-radius:8px;background:#ff3b6918;color:#ffc2d0;animation:ow-blink 1s infinite}
.ow-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(78px,1fr));gap:6px;margin:8px 0}
.ow-tile{border:1px solid #223457;border-radius:10px;padding:6px 3px;text-align:center;background:#0c1322;color:#5b6b88}
.ow-em{font-size:20px;line-height:1.15;filter:drop-shadow(0 0 6px currentColor)}.ow-nm{font-size:9px;color:#8aa0c4;margin-top:2px}.ow-st{font-size:9px;font-weight:900;margin-top:2px}.ow-ag{font-size:9px;color:#6f84a8;min-height:11px}
.ow-tile.s-ALIVE{border-color:#00f59b66;color:var(--g);box-shadow:0 0 10px #00f59b22;animation:ow-pulse 2s infinite}
.ow-tile.s-STALE{border-color:#f59e0b99;color:var(--a);animation:ow-blink 1s infinite}
.ow-tile.s-DEAD,.ow-tile.s-ERROR{border-color:var(--r);color:var(--r);box-shadow:0 0 14px #ff3b6944;animation:ow-blink .5s infinite,ow-shake .3s infinite}
.ow-tile.s-ZOMBIE{border-color:var(--p);color:var(--p);animation:ow-flicker 1.6s infinite}.ow-tile.s-UNKNOWN{opacity:.65}
.ow-players{display:grid;grid-template-columns:1fr 1fr;gap:8px}@media(max-width:560px){.ow-players{grid-template-columns:1fr}}
.ow-card{border:1px solid #223457;border-radius:12px;padding:8px;background:#0c1322}.ow-card.ready{border-color:var(--g);box-shadow:0 0 16px #00f59b33;animation:ow-pulse 2.4s infinite}.ow-card.no{border-color:#ff3b6988}
.ow-ch{display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:900}
.ow-badge{font-size:10px;padding:2px 7px;border-radius:7px;border:1px solid}.ow-card.ready .ow-badge{color:var(--g);border-color:var(--g);text-shadow:0 0 8px var(--g)}.ow-card.no .ow-badge{color:var(--r);border-color:var(--r);animation:ow-blink 1.2s infinite}
.ow-last{font-size:9px;color:#8aa0c4;margin-top:4px}.ow-chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.ow-chip{font-size:9px;padding:2px 5px;border-radius:6px;border:1px solid}.ow-chip.pass{color:var(--g);border-color:#00f59b55;background:#00f59b12}.ow-chip.warn{color:var(--a);border-color:#f59e0b66;background:#f59e0b12}.ow-chip.fail{color:var(--r);border-color:#ff3b6977;background:#ff3b6914}
.ow-xp{margin:10px 0 6px}.ow-xph{display:flex;justify-content:space-between;font-size:10px;margin-bottom:4px;color:#9fb4d6}
.ow-bar{height:10px;border-radius:6px;background:#111b30;border:1px solid #223457;overflow:hidden}.ow-bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--c),var(--g));box-shadow:0 0 10px var(--g);transition:width .6s}.ow-bar.full i{animation:ow-pulse 1s infinite}
.ow-log{display:flex;flex-direction:column;gap:3px;margin-top:6px}.ow-lh{font-size:10px;color:#9fb4d6;margin-top:8px}
.ow-row{display:grid;grid-template-columns:62px 18px 18px 56px 1fr auto;gap:6px;align-items:center;padding:4px 6px;border-radius:7px;background:#0c1322;border-left:3px solid #223457;font-size:10px}
.ow-row.win{border-left-color:var(--g)}.ow-row.loss{border-left-color:var(--r)}.ow-row.open{border-left-color:var(--c)}.ow-row.cancel{opacity:.5}.ow-row.shadow{background:#0d1426}.ow-row.new{animation:ow-flash 3s}
.ow-row .t{color:#8aa0c4}.ow-row .z{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#b9cdee}.ow-row .r{text-align:right;white-space:nowrap}.ow-row.win .r{color:var(--g)}.ow-row.loss .r{color:var(--r)}.ow-row .r small{color:#6f84a8;margin-left:4px}
.ow-empty{color:#5b6b88;padding:8px;text-align:center}
@keyframes ow-pulse{0%,100%{filter:brightness(1)}50%{filter:brightness(1.35)}}@keyframes ow-blink{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes ow-shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-1.5px)}75%{transform:translateX(1.5px)}}
@keyframes ow-flicker{0%,100%{opacity:1}8%{opacity:.4}12%{opacity:1}40%{opacity:.7}44%{opacity:1}70%{opacity:.5}}
@keyframes ow-flash{0%{background:#38bdf866}100%{background:#0c1322}}
@media(prefers-reduced-motion:reduce){.ow *{animation:none!important}}`;

  function mount(host, opts) {
    opts = opts || {};
    if (!document.getElementById('ow-css')) { const s = document.createElement('style'); s.id = 'ow-css'; s.textContent = CSS; document.head.appendChild(s); }
    host.innerHTML = `<div class="ow"><div class="ow-title"><span>🎮 COMMAND DECK</span><span class="ow-sp"></span><span class="ow-demo" hidden>DEMO</span><span class="ow-tag" data-k="clock"></span><span class="ow-led" data-k="led"></span></div>
<div class="ow-banner" data-k="banner" hidden></div><div class="ow-tiles" data-k="tiles"></div><div class="ow-players" data-k="players"></div>
<div class="ow-xp"><div class="ow-xph"><span data-k="xpl"></span><span data-k="xpr"></span></div><div class="ow-bar" data-k="bar"><i></i></div></div>
<div class="ow-lh">⚡ QUEST LOG · LATEST TRADES</div><div class="ow-log" data-k="log"></div></div>`;
    const q = (k) => host.querySelector(`[data-k="${k}"]`), api = (opts.api || '') + '/api/btc-15m/ops';
    const els = { tiles: {}, rows: {}, cards: {} };
    ORDER.forEach((n) => { const d = document.createElement('div'); d.className = 'ow-tile s-UNKNOWN'; d.dataset.s = 'UNKNOWN';
      d.innerHTML = `<div class="ow-em">${EMO[n]}</div><div class="ow-nm">${SHORT[n]}</div><div class="ow-st">NO SIGNAL</div><div class="ow-ag">--</div>`; q('tiles').appendChild(d); els.tiles[n] = d; });
    ['kalshi', 'polymarket'].forEach((v) => { const d = document.createElement('div'); d.className = 'ow-card no';
      d.innerHTML = `<div class="ow-ch"><span>${VEM[v]} ${VNM[v]}</span><span class="ow-badge">CHECKING</span></div><div class="ow-last"></div><div class="ow-chips"></div>`; q('players').appendChild(d); els.cards[v] = d; });
    let snap = null, fetchedAt = 0, okAt = 0, demo = false, sig = {}; const t0 = Date.now(); let dstate = null;

    async function poll() {
      try { const r = await fetch(api, { cache: 'no-store' }); if (!r.ok) throw new Error(r.status); snap = await r.json(); fetchedAt = Date.now(); okAt = fetchedAt; demo = false; }
      catch (e) { if (opts.demo || (!okAt && Date.now() - t0 > 2500)) { snap = demoSnap(); fetchedAt = Date.now(); demo = true; } }
      render();
    }
    function render() {
      q('clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      host.querySelector('.ow-demo').hidden = !demo;
      const blind = !demo && okAt && Date.now() - okAt > 6000, connecting = !snap;
      q('led').className = 'ow-led' + (blind || connecting ? ' off' : '');
      if (!snap) { q('banner').hidden = false; q('banner').textContent = '📡 CONNECTING TO OPS BACKEND…'; return; }
      const m = model(snap, (Date.now() - fetchedAt) / 1000);
      const ban = blind ? '📵 OPS BACKEND UNREACHABLE: WATCHDOG IS BLIND. TREAT EVERYTHING AS DOWN' : m.alerts.length ? '🚨 ' + m.alerts.join('  ·  ') : '';
      q('banner').hidden = !ban; if (q('banner').textContent !== ban) q('banner').textContent = ban;
      m.tiles.forEach((t) => { const d = els.tiles[t.name], st = blind ? 'UNKNOWN' : t.status;
        if (d.dataset.s !== st) { d.dataset.s = st; d.className = 'ow-tile s-' + st; }
        const txt = (SICO[st] ? SICO[st] + ' ' : '') + STXT[st], a = d.children[2], b = d.children[3];
        if (a.textContent !== txt) a.textContent = txt; const at = blind ? '--' : (t.idleTxt || t.ageTxt) + (t.crashes ? ' · ' + t.crashes + '💥' : ''); if (b.textContent !== at) b.textContent = at; d.title = t.detail; });
      ['kalshi', 'polymarket'].forEach((v) => { const p = m.players[v], d = els.cards[v], s = JSON.stringify([p.ready, p.checks.map((c) => c.level + c.detail)]);
        d.className = 'ow-card ' + (p.ready ? 'ready' : 'no');
        d.children[0].children[1].textContent = p.ready ? '✅ READY TO ROLL' : '⛔ NOT READY (' + p.fails + ')';
        d.children[1].textContent = (p.sleepy ? '😴 ' : '🔥 ') + 'LIVE ' + p.liveTxt + ' · 👻 SHADOW ' + p.shadowTxt;
        if (sig[v] !== s) { sig[v] = s; d.children[2].innerHTML = p.checks.map((c) => `<span class="ow-chip ${c.level}" title="${String(c.detail || '').replace(/"/g, '')}">${CHK[c.level]} ${c.label.toUpperCase()}</span>`).join(''); } });
      q('xpl').textContent = '👻 SHADOW XP · GRADED ' + m.xp.graded + '/' + m.xp.target + (m.xp.unlocked ? ' · 🔓 GATE UNLOCKED' : ' · 🔒 GATE');
      q('xpr').textContent = m.xp.wins + 'W-' + m.xp.losses + 'L ' + money(m.xp.pnl);
      q('bar').firstChild.style.width = m.xp.pct + '%'; q('bar').className = 'ow-bar' + (m.xp.unlocked ? ' full' : '');
      const log = q('log'), seen = {};
      if (!m.rows.length && !log.children.length) log.innerHTML = '<div class="ow-empty">NO TRADES YET 🎯</div>';
      else if (m.rows.length && log.firstChild && log.firstChild.className === 'ow-empty') log.innerHTML = '';
      for (let i = m.rows.length - 1; i >= 0; i--) { const r = m.rows[i]; seen[r.id] = 1; let d = els.rows[r.id];
        if (!d) { d = document.createElement('div'); els.rows[r.id] = d; d.dataset.s = ''; if (snapReady) d.className = 'ow-row new'; log.insertBefore(d, log.firstChild); setTimeout(() => d.classList.remove('new'), 3100); }
        const key = r.state + r.res + r.ago;
        if (d.dataset.s !== key) { d.dataset.s = key; const cls = 'ow-row ' + r.state + ' ' + r.mode + (d.classList.contains('new') ? ' new' : ''); d.className = cls;
          d.innerHTML = `<span class="t">${r.time}</span><span>${r.vem}</span><span>${r.mem}</span><span>${r.sem}</span><span class="z">${String(r.size).replace(/</g, '')}</span><span class="r">${r.res}<small>${r.ago}</small></span>`; } }
      Object.keys(els.rows).forEach((id) => { if (!seen[id]) { els.rows[id].remove(); delete els.rows[id]; } });
      snapReady = true;
    }
    let snapReady = false;
    function demoSnap() {
      const t = Date.now() / 1000; dstate = dstate || { trades: [], n: 0, last: 0 };
      if (t - dstate.last > 7) { dstate.last = t; dstate.n++; const v = dstate.n % 2 ? 'kalshi' : 'polymarket', sh = dstate.n % 3 !== 0;
        dstate.trades.unshift({ id: 'd' + dstate.n, venue: v, mode: sh ? 'shadow' : 'live', side: Math.random() < .5 ? 'UP' : 'DOWN', qty: 5 + Math.floor(Math.random() * 20), cents: 40 + Math.floor(Math.random() * 25), ts: t, bot: sh ? 'shadow-v2' : 'alpha', status: 'open' }); }
      dstate.trades.forEach((x) => { if (x.status === 'open' && !x.result && t - x.ts > 20) { x.result = Math.random() < .55 ? 'WIN' : 'LOSS'; x.pnl = x.result === 'WIN' ? x.qty * (1 - x.cents / 100) : -x.qty * x.cents / 100; } });
      const comp = ORDER.map((n) => ({ name: n, label: SHORT[n], emoji: EMO[n], status: n === 'shadow-engine' ? 'ZOMBIE' : n === 'polymarket-exec' ? 'STALE' : 'ALIVE', ageSec: n === 'polymarket-exec' ? 41 : 1 + Math.random() * 3, progressAgeSec: n === 'shadow-engine' ? 1500 : 2, crashes: n === 'brain' ? 2 : 0, detail: '' }));
      const gr = dstate.trades.filter((x) => x.mode === 'shadow' && x.result), ok = (id, l, lv, d) => ({ id, label: l, level: lv, detail: d });
      const baseChecks = (bad) => [ok('feed', 'Price feed', 'pass'), ok('exec', 'Exec loop', bad ? 'warn' : 'pass'), ok('creds', 'Credentials', bad ? 'fail' : 'pass', bad ? 'probe not wired' : ''), ok('api', 'Exchange API', 'pass'), ok('market', 'Open 15m market', 'pass'), ok('balance', 'Balance', 'pass'), ok('strike', 'Round target', 'pass'), ok('lastTrade', 'Last live trade', 'warn', 'none recorded')];
      return { serverTimeMs: Date.now(), components: comp, alerts: ['👻 Shadow engine ZOMBIE'], trades: dstate.trades.slice(0, 40),
        stats: { live: {}, shadow: { n: dstate.trades.length, graded: 112 + gr.length, wins: 66 + gr.filter((x) => x.result === 'WIN').length, losses: 46 + gr.filter((x) => x.result === 'LOSS').length, pnl: 18.4, gateTarget: 150 } },
        lastTradeAgeSec: { kalshi: { live: 3 * 86400, shadow: 14 }, polymarket: { live: null, shadow: 22 } },
        readiness: { kalshi: { ready: true, checks: baseChecks(false) }, polymarket: { ready: false, checks: baseChecks(true) } }, events: [] };
    }
    poll(); setInterval(poll, 2000); setInterval(() => snap && render(), 1000);
    return { refresh: poll };
  }
  const api = { mount, model };
  if (typeof window !== 'undefined') window.OpsWidget = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
