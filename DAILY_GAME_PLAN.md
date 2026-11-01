# BTC-Edge — Daily Game Plan & Strategy (Autonomous)

> Written by the agent on 2026-11-01. This is the live, single source of truth for
> how the bot thinks and trades. It runs 24/7 with no human input; this document is
> the "homework" that records the algorithm, the daily plan, and the open work.

---

## 1. What it trades

| Venue | Market | Window | Settlement |
|-------|--------|--------|-----------|
| **Kalshi** | `KXBTC15M` — "BTC up in 15 min?" | 15m (UTC-aligned) | BRTI 60s average vs open |
| **Polymarket** | `cpc-btc-updown-15m-{ts}z` — BTC up/down | 15m (UTC-aligned) | oracle vs open |

Both settle the **same underlying** (BTC up/down), so the "correct answer" is identical.
They get **separate models** because each has its own order book (crowd sentiment)
and its own learning history. That separation is the whole point: we read each
market's *own* signal instead of averaging the two together.

---

## 2. The algorithm (the "brain")

Per venue, a small ensemble:
- **Online logistic regression** (SGD, `partial_fit`) — updated every resolved window.
- **LightGBM** — retrained on accumulated (feature → outcome) history.

Features (fixed order): `window_delta_pct`, `option_implied_prob`, `book_imbalance`,
`micro_acceleration`, `news_sentiment`, `time_remaining`, `regime`, `contract_price`,
`realized_vol`, `implied_vol`.

**Learning loop (fully autonomous, no gate):**
1. At each 15m window rollover, resolve the outcome (`spot >= open_price` → up/down).
2. Record it to `btc_15m_window_evaluations` (venue-scoped) — the persistent brain.
3. `ens.update(features, outcome)` + `retrain_lgbm()`.
4. Append every signal + settlement to `data/trades.jsonl` (permanent audit trail).
5. Telegram "brain update".

**Warm-start:** Kalshi seeds on ~200 real settled windows (BTC move + outcome);
Polymarket seeds on resolved Gamma outcomes (base rate).

---

## 3. The risk & edge rules (the guardrails)

All in `config/config.yaml` under `trading:`

| Knob | Value | Meaning |
|------|-------|---------|
| `quarter_kelly_cap_pct` | 2.0 | max 2% of balance per single bet |
| `max_exposure_pct` | 35.0 | max 35% of balance in open positions per venue |
| `polymarket_cap_pct` | 2.0 | Polymarket per-trade cap |
| `polymarket_exposure_pct` | 35.0 | Polymarket total exposure cap |
| `chase_guard_pct` | 0.1 | skip a side that already moved >0.1% (anti-chase) |
| `entry_warmup_seconds` | 60 | stay flat for the first 60s of a window |
| `take_profit_pct` | 85 | auto-sell when a position is +85%, then re-enter |
| `max_daily_loss_pct` | 10 | halt after −10% of bankroll (−$2 on $20) |
| `min_ev_threshold` | 0.01 | min expected-value edge to fire |

**Hard filters (in `size_for`):**
- `prob <= price` → **skip** (no zero/negative-edge bets — this killed the careless longshots).
- `price < 0.05 or > 0.95` → **skip** (no 1%-chance legs).

**Timing:** `entry_warmup` (first 60s flat) → strategies fire → `chase_guard` +
edge filter → route → hold → **take-profit at +85%** (Polymarket) or settle at close.

---

## 4. Daily game plan

The bot is event-driven, not calendar-driven, but the day shapes up like this:

1. **Every 15m window (96/day/venue):**
   - First 60s: flat (warmup, gather info).
   - Evaluate strategies; keep only positive-edge, non-chasing entries.
   - Open at 2% of live balance (Polymarket 5-share min / Kalshi 1-contract min).
   - Hold; take-profit at +85% on Polymarket; ride Kalshi to settlement.
   - At rollover: settle, learn, log, notify.
2. **Last minute of each window:** Telegram "closing soon" heads-up (both venues).
3. **Every order + every settlement:** logged to `data/trades.jsonl` + Telegram.
4. **Continuous:** portfolio poll every 10s; balance, fills, positions, P&L.

---

## 5. Known issues & the plan to fix

1. **Polymarket BBO 404** (`cpc-btc-updown-15m-{ts}z/bbo`) — the live feed can't
   read the real Polymarket book, so it falls back to a synthetic 50/50 book and a
   ~50% model → coin-flip minus fees = structural losses. **Fix:** confirm the
   correct gateway slug/timezone; this is the single highest-leverage item.
2. **Kalshi "down" needs margin** — selling YES (short) needs USDC margin (~$250),
   not available. The bot stands aside when the model says "down" on Kalshi.
3. **Polymarket historical seeding** returns few resolved outcomes (Gamma limits);
   Kalshi is the rich seed source.

---

## 6. Removed (useless / bad parts)

- Champion/Challenger promotion gate (confusing, added no value) → **direct learning**.
- The gate UI card + `/api/btc-15m/gate/evaluate` endpoint.
- Careless zero-edge & longshot bets (the `prob<=price` and `<5%/>95%` filters).
- The "enforce minimum even with no edge" behavior.

---

## 7. Homework (what to watch next)

- Watch the `edge-diag` log lines (`engine_err.log`) — model prob vs book vs edge.
- Watch `data/trades.jsonl` — cumulative win/loss per venue/strategy.
- If Polymarket keeps losing: it's the BBO 404, not the strategy — fix the slug.
- Tune `take_profit_pct` (85% → 50% for faster profit-taking) and
  `entry_warmup_seconds` (60 → 180 to wait longer) as behavior is observed.
